"""H3 TLS/JA3 Cloudflare Enterprise — auto-fallback firefox->chromium"""
import sys
import tempfile
import time as real_time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import core.profile_manager as pm

# isolate PROFILES_ROOT for standalone run (conftest will override per-test)
_tmp = Path(tempfile.mkdtemp(prefix="agentfox_tls_"))
pm.PROFILES_ROOT = _tmp

from core.profile_manager import Profile, create_profile, maybe_auto_fallback, auto_fallback_if_needed
from core.session import maybe_auto_fallback as sess_maybe_fallback, auto_fallback_if_needed as sess_auto_fallback
import api.server as server
from fastapi.testclient import TestClient




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
    def __init__(self, content="<html><body>plain</body></html>"):
        self.keyboard = FakeKeyboard()
        self.mouse = FakeMouse()
        self.navigated = []
        self._content = content
        self.url = "https://example.com"
        self.title = lambda: "Example"
    def goto(self, url, wait_until="domcontentloaded", timeout=30000):
        self.navigated.append(url)
        self.url = url
    def content(self):
        return self._content
    def evaluate(self, js):
        if "out.push" in js and "tree" in js:
            return {"tree": [], "url": self.url, "title": "Example"}
        if "innerWidth" in js:
            return {"x": 640.0, "y": 360.0}
        if "innerText" in js:
            return 400
        return None
    def locator(self, selector):
        return FakeLocator()
    def fill(self, selector, text):
        pass
    def screenshot(self, type="png", full_page=False):
        return b"fake_png"
    def pdf(self, format="a4", landscape=False):
        return b"fake_pdf"

class FakeEngine:
    def __init__(self, content="<html><body>plain</body></html>"):
        self.page = FakePage(content)
    def launch(self, profile, headless=True):
        return self.page
    def close(self):
        pass


def _clear_sessions():
    # clear global sessions and locks that may linger
    for sid, sess in list(server._sessions.items()):
        try:
            sess["engine"].close()
        except Exception:
            pass
    server._sessions.clear()


def test_auto_fallback_on_blocked():
    _clear_sessions()
    # create firefox profile
    p = create_profile("tls_firefox_1", geo="DE", engine="firefox")
    assert p.engine == "firefox"
    # record 3 blocked signals → status banned
    for i in range(3):
        p.health.record_signal("blocked", f"https://example.com/{i}")
    # ensure health is banned
    assert p.health.status == "banned", f"status {p.health.status}"
    assert p.health.consecutive_failures >= 3
    p.save()
    # also test both import paths exist
    assert callable(maybe_auto_fallback)
    assert callable(auto_fallback_if_needed)
    assert callable(sess_maybe_fallback)
    assert callable(sess_auto_fallback)

    # call maybe_auto_fallback — should switch to chromium
    switched = maybe_auto_fallback(p)
    assert switched is True, "should switch on banned"
    assert p.engine == "chromium", f"engine after fallback {p.engine}"
    # check persisted
    fresh = Profile.load("tls_firefox_1")
    assert fresh.engine == "chromium", f"persisted engine {fresh.engine}"
    # second call should not switch again (avoid infinite loop)
    switched2 = maybe_auto_fallback(p)
    assert switched2 is False, "second call should be no-op"
    assert p.engine == "chromium"

    # chromium profile should not fallback
    p_ch = create_profile("tls_chromium_1", geo="DE", engine="chromium")
    for _ in range(3):
        p_ch.health.record_signal("blocked", "https://example.com/")
    p_ch.save()
    assert p_ch.health.status == "banned"
    switched_ch = auto_fallback_if_needed(p_ch)
    assert switched_ch is False
    assert p_ch.engine == "chromium"

    # using session re-export
    p2 = create_profile("tls_firefox_2", geo="DE", engine="firefox")
    for _ in range(3):
        p2.health.record_signal("suspicious", "https://example.com/")
    p2.save()
    assert p2.health.status in ("degraded", "banned")
    sw = sess_auto_fallback(p2)
    assert sw is True
    assert p2.engine == "chromium"


def test_api_check_signals_triggers_fallback():
    _clear_sessions()
    # stub time if not already stubbed (avoid 1.5s sleep)
    orig_time = getattr(server, "time", None)
    # detect if already stubbed (has slept attr)
    need_stub = not hasattr(orig_time, "slept")
    stub = None
    bmouse = bscroll = btiming = None
    if need_stub:
        class _Stub:
            def __init__(self):
                self._t = real_time.time()
                self.slept = 0.0
            def sleep(self, s):
                self.slept += s
                self._t += s
            def time(self):
                return self._t
        stub = _Stub()
        orig_bmouse_time = None
        orig_bscroll_time = None
        orig_btiming_time = None
        try:
            import behavior.mouse as _bm, behavior.scroll as _bs, behavior.timing as _bt
            bmouse, bscroll, btiming = _bm, _bs, _bt
            orig_bmouse_time = _bm.time
            orig_bscroll_time = _bs.time
            orig_btiming_time = _bt.time
            server.time = stub
            _bm.time = stub
            _bs.time = stub
            _bt.time = stub
        except Exception:
            pass
    else:
        stub = orig_time

    # prepare FakeEngine with blocked content
    blocked_html = "<html><body>Access denied - You have been blocked by network security</body></html>"
    # need at least 1 blocked to get degraded; but our helper triggers on >=1
    # to guarantee fallback, we will set content that triggers blocked and also pre-seed health with 1 failure
    # Actually our _check_signals will record_signal and then fallback should happen.
    engine = FakeEngine(content=blocked_html)
    orig_get_engine = server.get_engine
    server.get_engine = lambda p: engine

    client = TestClient(server.app, raise_server_exceptions=False)

    # create firefox profile via API
    r = client.post("/profiles", json={"id": "tls_api_firefox", "geo": "DE", "engine": "firefox"})
    assert r.status_code == 201, r.text
    # start session
    # ensure the FakeEngine's page is the one with blocked content
    engine.page = FakePage(content=blocked_html)
    r = client.post("/sessions/tls_api_firefox/start", json={"headless": True})
    assert r.status_code == 200, r.text
    sid = r.json()["session_id"]
    assert r.json()["engine"] == "firefox"

    # need to prepare health so that after one goto, consecutive_failures will be at least 1 and status degraded
    # _check_signals will record blocked -> degraded, then fallback
    # But if helper requires failures >=2, one goto wouldn't fallback. We handle that by ensuring health already has 1 prior failure
    # So pre-seed one blocked signal before goto to make total 2
    p_before = Profile.load("tls_api_firefox")
    p_before.health.record_signal("blocked", "https://pre.example.com/")
    p_before.save()
    # now p is degraded with 1 failure, next goto will make 2
    # update session's profile reference to fresh
    # session stores reference to p object from start; it may be stale. Reload and update _sessions
    fresh = Profile.load("tls_api_firefox")
    server._sessions[sid]["profile"] = fresh
    # ensure engine page still blocked
    engine.page._content = blocked_html
    # also update server._sessions profile health reference
    # call goto
    r = client.post(f"/sessions/{sid}/goto", json={"url": "https://example.com/blocked", "read": False})
    # should succeed and contain signals
    assert r.status_code == 200, r.text
    data = r.json()
    # should have detected blocked
    assert "signals" in data or "health" in data or data.get("ok"), data
    # check profile engine switched
    p_after = Profile.load("tls_api_firefox")
    assert p_after.engine == "chromium", f"engine after api goto should be chromium, got {p_after.engine} health {p_after.health.status} signals {p_after.health.signals}"
    # also session's in-memory profile should be updated
    assert server._sessions[sid]["profile"].engine == "chromium"

    # second goto should not switch again (already chromium)
    engine.page._content = blocked_html
    r2 = client.post(f"/sessions/{sid}/goto", json={"url": "https://example.com/blocked2"})
    # even with blocked again, should not switch back to firefox
    p_after2 = Profile.load("tls_api_firefox")
    assert p_after2.engine == "chromium"

    # cleanup
    client.post(f"/sessions/{sid}/stop")
    server.get_engine = orig_get_engine
    _clear_sessions()
    # restore time stub if we created one
    if need_stub:
        try:
            server.time = orig_time
            if bmouse is not None:
                import behavior.mouse as _bm, behavior.scroll as _bs, behavior.timing as _bt
                _bm.time = orig_bmouse_time
                _bs.time = orig_bscroll_time
                _bt.time = orig_btiming_time
        except Exception:
            pass
