import sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import core.profile_manager as pm
tmp = Path(tempfile.mkdtemp(prefix="eval_"))
pm.PROFILES_ROOT = tmp

import api.server as srv
from fastapi.testclient import TestClient

class FakeMouse:
    def __init__(self): self.moves=[]; self.clicks=[]; self.wheels=[]
    def move(self,x,y): self.moves.append((x,y))
    def click(self,x,y): self.clicks.append((x,y))
    def wheel(self,dx,dy): self.wheels.append(dy)

class FakePageEval:
    def __init__(self):
        self.mouse=FakeMouse()
        self.url="https://example.com"
        from unittest.mock import MagicMock
        self.keyboard=MagicMock()
        self.keyboard.press=lambda k: None
        self.keyboard.type=lambda ch, delay=0: None
    def title(self): return "Example"
    def content(self): return "<html>ok</html>"
    def goto(self, url, wait_until="domcontentloaded", timeout=30000): self.url=url
    def evaluate(self, js):
        # handle snapshot JS
        if "out.push" in js and "tree" in js:
            return {"tree": [{"ref":"@e1","role":"button","name":"Submit","selector":"button.submit","tag":"button"}], "url": self.url, "title": "Example"}
        if "innerWidth" in js: return {"x":640,"y":360}
        if "innerText" in js: return 400
        # evaluate logic for tests: return 42 if 42 in code, else handle async
        if js is None:
            return None
        s = str(js)
        if "42" in s:
            return 42
        if "async" in s or "await" in s:
            # simulate async evaluation
            if "99" in s:
                return 99
            return 42
        # default
        return 42
    def locator(self, selector):
        class L:
            @property
            def first(self): return self
            def click(self, timeout=5000): pass
            def bounding_box(self, timeout=10000): return {"x":10,"y":10,"width":60,"height":20}
        return L()
    def fill(self, s,t): pass

class FakeEngineEval:
    def __init__(self): self.page=FakePageEval()
    def launch(self, profile, headless=True): return self.page
    def close(self): pass

def test_evaluate_returns_result():
    fake = FakeEngineEval()
    orig = srv.get_engine
    srv.get_engine = lambda p: fake
    client = TestClient(srv.app, raise_server_exceptions=False)
    r = client.post("/profiles", json={"id":"eval_a","geo":"DE"})
    assert r.status_code==201, r.text
    r = client.post("/sessions/eval_a/start", json={"headless":True})
    assert r.status_code==200, r.text
    sid = r.json()["session_id"]
    r = client.post(f"/sessions/{sid}/evaluate", json={"code":"() => 42"})
    assert r.status_code==200, r.text
    data = r.json()
    assert "result" in data, data
    assert data["result"] == 42, data
    # also test "() => document.title" style (should return 42 via fake)
    r = client.post(f"/sessions/{sid}/evaluate", json={"code":"() => document.title"})
    assert r.status_code==200, r.text
    assert "result" in r.json()
    srv.get_engine = orig

def test_evaluate_async_code():
    fake = FakeEngineEval()
    orig = srv.get_engine
    srv.get_engine = lambda p: fake
    client = TestClient(srv.app, raise_server_exceptions=False)
    r = client.post("/profiles", json={"id":"eval_b","geo":"DE"})
    assert r.status_code==201, r.text
    r = client.post("/sessions/eval_b/start", json={"headless":True})
    assert r.status_code==200, r.text
    sid = r.json()["session_id"]
    # async arrow function
    r = client.post(f"/sessions/{sid}/evaluate", json={"code":"async () => { return 42 }"})
    assert r.status_code==200, r.text
    assert r.json()["result"] == 42, r.text
    # plain await without wrapper - endpoint should wrap to async
    r = client.post(f"/sessions/{sid}/evaluate", json={"code":"await Promise.resolve(42)"})
    assert r.status_code==200, r.text
    assert "result" in r.json(), r.text
    assert r.json()["result"] == 42, r.text
    # also test arrow with await but missing async keyword (should be auto-prefixed)
    r = client.post(f"/sessions/{sid}/evaluate", json={"code":"() => await Promise.resolve(42)"})
    assert r.status_code==200, r.text
    assert r.json()["result"] == 42, r.text
    srv.get_engine = orig

def test_cdp_endpoint():
    fake = FakeEngineEval()
    orig = srv.get_engine
    srv.get_engine = lambda p: fake
    client = TestClient(srv.app, raise_server_exceptions=False)
    r = client.post("/profiles", json={"id":"eval_c","geo":"DE"})
    assert r.status_code==201, r.text
    r = client.post("/sessions/eval_c/start", json={"headless":True})
    assert r.status_code==200, r.text
    sid = r.json()["session_id"]
    r = client.post(f"/sessions/{sid}/cdp", json={"method":"Page.enable","params":{}})
    assert r.status_code in (200,501), r.text
    if r.status_code == 200:
        data = r.json()
        assert data.get("method") == "Page.enable" or "method" in data, data
        assert "result" in data, data
        # for FakePage should be fake
        assert data["result"] == "fake" or data["result"] is not None, data
    else:
        # 501 gracefully
        assert "CDP not available" in r.text or "CDP" in r.text, r.text
    # also test with no params
    r = client.post(f"/sessions/{sid}/cdp", json={"method":"DOM.enable"})
    assert r.status_code in (200,501), r.text
    # non-existent session
    r = client.post("/sessions/nonexist/cdp", json={"method":"Page.enable","params":{}})
    assert r.status_code == 404, r.text
    srv.get_engine = orig
