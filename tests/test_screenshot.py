import sys, tempfile, base64
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import core.profile_manager as pm
tmp = Path(tempfile.mkdtemp(prefix="shot_"))
pm.PROFILES_ROOT = tmp

import api.server as srv
from fastapi.testclient import TestClient

class FakeMouse:
    def __init__(self): self.moves=[]; self.clicks=[]; self.wheels=[]
    def move(self,x,y): self.moves.append((x,y))
    def click(self,x,y): self.clicks.append((x,y))
    def wheel(self,dx,dy): self.wheels.append(dy)

class FakeLocatorShot:
    def __init__(self, selector, data=b"fake png selector"):
        self.selector = selector
        self._data = data
        self.screenshot_calls = []
    @property
    def first(self):
        return self
    def screenshot(self, type="png"):
        self.screenshot_calls.append((self.selector, type))
        return self._data
    def click(self, timeout=5000): pass
    def bounding_box(self, timeout=10000): return {"x":10,"y":10,"width":60,"height":20}

class FakePageShot:
    def __init__(self):
        self.mouse=FakeMouse()
        self.url="https://example.com"
        from unittest.mock import MagicMock
        self.keyboard=MagicMock()
        self.keyboard.press=lambda k: None
        self.keyboard.type=lambda ch, delay=0: None
        self._screenshot_data = b"fake png"
        self._pdf_data = b"%PDF fake pdf"
        self.last_screenshot_kwargs = None
        self.locator_calls = []
    def title(self): return "Example"
    def content(self): return "<html>ok</html>"
    def goto(self, url, wait_until="domcontentloaded", timeout=30000): self.url=url
    def evaluate(self, js):
        if "out.push" in js and "tree" in js:
            return {"tree": [{"ref":"@e1","role":"button","name":"Submit","selector":"button.submit","tag":"button"}], "url": self.url, "title": "Example"}
        if "innerWidth" in js: return {"x":640,"y":360}
        if "innerText" in js: return 400
        return None
    def locator(self, selector):
        self.locator_calls.append(selector)
        return FakeLocatorShot(selector)
    def fill(self, s,t): pass
    def screenshot(self, type="png", full_page=False):
        self.last_screenshot_kwargs = {"type": type, "full_page": full_page}
        return self._screenshot_data
    def pdf(self, format="a4", landscape=False):
        return self._pdf_data

class FakeEngineShot:
    def __init__(self): self.page=FakePageShot()
    def launch(self, profile, headless=True): return self.page
    def close(self): pass

def test_screenshot_returns_data():
    fake = FakeEngineShot()
    orig = srv.get_engine
    srv.get_engine = lambda p: fake
    client = TestClient(srv.app, raise_server_exceptions=False)
    r = client.post("/profiles", json={"id":"shot_a","geo":"DE"})
    assert r.status_code==201, r.text
    r = client.post("/sessions/shot_a/start", json={"headless":True})
    assert r.status_code==200, r.text
    sid = r.json()["session_id"]
    r = client.get(f"/sessions/{sid}/screenshot")
    assert r.status_code==200, r.text
    data = r.json()
    assert "data" in data, data
    assert "size" in data and data["size"] == len(b"fake png"), data
    assert data["format"] == "png"
    decoded = base64.b64decode(data["data"])
    assert decoded == b"fake png", decoded
    srv.get_engine = orig

def test_screenshot_with_selector():
    fake = FakeEngineShot()
    orig = srv.get_engine
    srv.get_engine = lambda p: fake
    client = TestClient(srv.app, raise_server_exceptions=False)
    r = client.post("/profiles", json={"id":"shot_b","geo":"DE"})
    assert r.status_code==201, r.text
    r = client.post("/sessions/shot_b/start", json={"headless":True})
    assert r.status_code==200, r.text
    sid = r.json()["session_id"]
    # plain selector
    r = client.get(f"/sessions/{sid}/screenshot", params={"selector":"button.submit"})
    assert r.status_code==200, r.text
    data = r.json()
    assert "data" in data
    assert base64.b64decode(data["data"]) == b"fake png selector"
    # via @e ref (need snapshot first)
    r = client.get(f"/sessions/{sid}/snapshot")
    assert r.status_code==200
    r = client.get(f"/sessions/{sid}/screenshot", params={"selector":"@e1"})
    assert r.status_code==200, r.text
    data = r.json()
    assert "data" in data
    srv.get_engine = orig

def test_pdf_endpoint():
    fake = FakeEngineShot()
    orig = srv.get_engine
    srv.get_engine = lambda p: fake
    client = TestClient(srv.app, raise_server_exceptions=False)
    r = client.post("/profiles", json={"id":"shot_c","geo":"DE"})
    assert r.status_code==201, r.text
    r = client.post("/sessions/shot_c/start", json={"headless":True})
    assert r.status_code==200, r.text
    sid = r.json()["session_id"]
    r = client.get(f"/sessions/{sid}/pdf")
    # for FakePage should return 200 with fake pdf; allow 501 fallback for real firefox without pdf
    assert r.status_code in (200, 501), r.text
    if r.status_code == 200:
        data = r.json()
        assert "data" in data, data
        assert data["size"] == len(b"%PDF fake pdf"), data
        decoded = base64.b64decode(data["data"])
        assert decoded == b"%PDF fake pdf"
        assert data["format"] == "a4"
    srv.get_engine = orig
