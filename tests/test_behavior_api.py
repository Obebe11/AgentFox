"""Тесты 2.4: behavior встроен во все API-эндпоинты (type/scroll/goto) — TestClient + fake engine."""
import sys
import tempfile
import time as real_time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import core.profile_manager as pm

pm.PROFILES_ROOT = Path(tempfile.mkdtemp(prefix="agentfox_t24_"))

import api.server as server
import behavior.mouse as bmouse
import behavior.scroll as bscroll
import behavior.timing as btiming
from fastapi.testclient import TestClient

client = TestClient(server.app, raise_server_exceptions=False)


class StubTime:
    def __init__(self):
        self._t = real_time.time()
        self.slept = 0.0

    def sleep(self, s):
        self.slept += s
        self._t += s

    def time(self):
        return self._t


stub = StubTime()
server.time = stub
bmouse.time = stub
bscroll.time = stub
btiming.time = stub


class FakeKeyboard:
    def __init__(self):
        self.events = []

    def press(self, key):
        self.events.append(("press", key))

    def type(self, ch, delay=0):
        self.events.append(("type", ch))


class FakeLocator:
    @property
    def first(self):
        return self

    def click(self, timeout=5000):
        pass

    def bounding_box(self, timeout=10000):
        return {"x": 10.0, "y": 10.0, "width": 60.0, "height": 20.0}


class FakeMouse:
    def __init__(self):
        self.moves = []
        self.clicks = []
        self.wheels = []

    def move(self, x, y):
        self.moves.append((x, y))

    def click(self, x, y):
        self.clicks.append((x, y))

    def wheel(self, dx, dy):
        self.wheels.append(dy)


class FakePage:
    def __init__(self, content="<html><body>plain page text</body></html>"):
        self.keyboard = FakeKeyboard()
        self.mouse = FakeMouse()
        self.navigated = []
        self._content = content

    def goto(self, url, wait_until="domcontentloaded", timeout=30000):
        self.navigated.append(url)

    def content(self):
        return self._content

    def evaluate(self, js):
        if "innerWidth" in js:
            return {"x": 640.0, "y": 360.0}
        if "innerText" in js:
            return 400
        return None

    def locator(self, selector):
        return FakeLocator()

    def fill(self, selector, text):
        pass


class FakeEngine:
    def __init__(self):
        self.page = FakePage()

    def launch(self, profile, headless=True):
        return self.page

    def close(self):
        pass


engine = FakeEngine()
server.get_engine = lambda p: engine


def _start(pid, content=None):
    engine.page = FakePage(content or "<html><body>plain page text</body></html>")
    r = client.post("/profiles", json={"id": pid, "geo": "DE"})
    assert r.status_code == 201, r.text
    r = client.post(f"/sessions/{pid}/start", json={"headless": True})
    assert r.status_code == 200, r.text
    return r.json()["session_id"]


def test_click_human_still_works():
    sid = _start("beh_click")
    r = client.post(f"/sessions/{sid}/click", json={"selector": "#btn"})
    assert r.status_code == 200 and r.json()["ok"], r.text
    assert engine.page.mouse.clicks, "click must go through mouse"
    assert len(engine.page.mouse.moves) > 10, "bezier movement expected"


def test_type_human_keystrokes_plus_pause_and_signals_clean():
    sid = _start("beh_type")
    page = engine.page
    s0 = stub.slept
    r = client.post(f"/sessions/{sid}/type", json={"selector": "#q", "text": "hi"})
    assert r.status_code == 200 and r.json()["ok"], r.text
    typed = "".join(ch for kind, ch in page.keyboard.events if kind == "type")
    assert typed == "hi"
    assert stub.slept > s0, "post-type human pause expected"


def test_type_detects_captcha_and_cooldowns():
    sid = _start("beh_sig", content="<html>Please solve this captcha to continue</html>")
    r = client.post(f"/sessions/{sid}/type", json={"selector": "#q", "text": "hi"})
    data = r.json()
    assert "captcha" in data["signals"], data
    h = client.get("/health/beh_sig").json()
    assert h["health"]["status"] == "cooldown"
    client.post(f"/sessions/{sid}/stop")
    locked, reason = pm.Profile.load("beh_sig").is_locked()
    assert locked and "cooldown" in reason, reason


def test_goto_read_mode_triggers_warmup_visit():
    sid = _start("beh_goto")
    page = engine.page
    s0 = stub.slept
    w0 = len(page.mouse.wheels)
    r = client.post(f"/sessions/{sid}/goto", json={"url": "https://example.com/", "read": True})
    assert r.status_code == 200 and r.json()["ok"], r.text
    assert page.navigated[-1] == "https://example.com/"
    assert len(page.mouse.wheels) > w0, "read mode must scroll lightly"
    assert stub.slept - s0 > 2.0, "reading pauses expected"


def test_goto_default_skips_read_behavior():
    sid = _start("beh_goto2")
    page = engine.page
    s0 = stub.slept
    w0 = len(page.mouse.wheels)
    r = client.post(f"/sessions/{sid}/goto", json={"url": "https://example.com/"})
    assert r.status_code == 200 and r.json()["ok"], r.text
    assert len(page.mouse.wheels) == w0, "no scrolling without read=True"
    assert stub.slept - s0 < 4.0, "only pre-pause + tracker wait expected"


def test_scroll_detour_full_probability_fires():
    sid = _start("beh_scroll")
    calls = []
    orig = server.maybe_detour

    def spy(page, p):
        calls.append(p)
        return orig(page, p)

    server.maybe_detour = spy
    try:
        r = client.post(f"/sessions/{sid}/scroll", json={"screens": 1, "detour": 1.0})
        assert r.status_code == 200 and r.json()["ok"], r.text
        assert calls == [1.0], "detour must be invoked with given probability"
        assert any(dy < 0 for dy in engine.page.mouse.wheels), "detour scrolls up"
    finally:
        server.maybe_detour = orig


def test_scroll_detour_zero_never_detours():
    sid = _start("beh_scroll0")
    saved = server.natural_scroll
    server.natural_scroll = lambda *a, **k: None
    try:
        w0 = list(engine.page.mouse.wheels)
        r = client.post(f"/sessions/{sid}/scroll", json={"detour": 0.0})
        assert r.status_code == 200 and r.json()["ok"], r.text
        assert engine.page.mouse.wheels == w0, "detour=0 must be a no-op"
    finally:
        server.natural_scroll = saved


def test_scroll_detects_blocked_signal():
    sid = _start("beh_blk", content="<html>Access denied — you have been blocked</html>")
    r = client.post(f"/sessions/{sid}/scroll", json={"detour": 0.0})
    data = r.json()
    assert "blocked" in data["signals"], data
    h = client.get("/health/beh_blk").json()
    assert h["health"]["status"] in ("cooldown", "degraded")


def test_stop_releases_lock_and_session():
    sid = _start("beh_stop")
    r = client.post(f"/sessions/{sid}/stop")
    assert r.status_code == 200 and r.json()["ok"], r.text
    assert all(s["session_id"] != sid for s in client.get("/sessions").json())
    locked, _ = pm.Profile.load("beh_stop").is_locked()
    assert not locked
