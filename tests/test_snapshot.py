import sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import core.profile_manager as pm
tmp = Path(tempfile.mkdtemp(prefix="snap_"))
pm.PROFILES_ROOT = tmp

import api.server as srv
from fastapi.testclient import TestClient

class FakeMouse:
    def __init__(self): self.moves=[]; self.clicks=[]; self.wheels=[]
    def move(self,x,y): self.moves.append((x,y))
    def click(self,x,y): self.clicks.append((x,y))
    def wheel(self,dx,dy): self.wheels.append(dy)

class FakePageSnap:
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
        if "out.push" in js and "tree" in js:
            return {"tree": [{"ref":"@e1","role":"button","name":"Submit","selector":"button.submit","tag":"button"},{"ref":"@e2","role":"textbox","name":"Search","selector":"input#q","tag":"input"}], "url": self.url, "title": "Example"}
        if "innerWidth" in js: return {"x":640,"y":360}
        if "innerText" in js: return 400
        return None
    def locator(self, selector):
        class L:
            @property
            def first(self): return self
            def click(self, timeout=5000): pass
            def bounding_box(self, timeout=10000): return {"x":10,"y":10,"width":60,"height":20}
        return L()
    def fill(self, s,t): pass

class FakeEngineSnap:
    def __init__(self): self.page=FakePageSnap()
    def launch(self, profile, headless=True): return self.page
    def close(self): pass

def test_snapshot_returns_tree_and_refs():
    fake = FakeEngineSnap()
    orig = srv.get_engine
    srv.get_engine = lambda p: fake
    client = TestClient(srv.app, raise_server_exceptions=False)
    r = client.post("/profiles", json={"id":"snap_a","geo":"DE"})
    assert r.status_code==201
    r = client.post("/sessions/snap_a/start", json={"headless":True})
    sid = r.json()["session_id"]
    r = client.get(f"/sessions/{sid}/snapshot")
    assert r.status_code==200
    data = r.json()
    assert "tree" in data and len(data["tree"])==2
    assert data["tree"][0]["ref"]=="@e1"
    assert data["tree"][0]["selector"]=="button.submit"
    # check refs stored
    assert srv._sessions[sid]["snapshot_refs"]["@e1"]=="button.submit"
    srv.get_engine = orig

def test_click_with_e_ref():
    fake = FakeEngineSnap()
    orig = srv.get_engine
    srv.get_engine = lambda p: fake
    client = TestClient(srv.app, raise_server_exceptions=False)
    r = client.post("/profiles", json={"id":"snap_b","geo":"DE"})
    assert r.status_code==201
    r = client.post("/sessions/snap_b/start", json={"headless":True})
    sid = r.json()["session_id"]
    # need snapshot first to populate refs
    client.get(f"/sessions/{sid}/snapshot")
    r = client.post(f"/sessions/{sid}/click", json={"selector":"@e1"})
    assert r.status_code==200 and r.json()["ok"]
    # also CSS still works
    r = client.post(f"/sessions/{sid}/click", json={"selector":"button.submit"})
    assert r.status_code==200
    srv.get_engine = orig

def test_type_with_e_ref():
    fake = FakeEngineSnap()
    orig = srv.get_engine
    srv.get_engine = lambda p: fake
    client = TestClient(srv.app, raise_server_exceptions=False)
    r = client.post("/profiles", json={"id":"snap_c","geo":"DE"})
    assert r.status_code==201
    r = client.post("/sessions/snap_c/start", json={"headless":True})
    sid = r.json()["session_id"]
    client.get(f"/sessions/{sid}/snapshot")
    r = client.post(f"/sessions/{sid}/type", json={"selector":"@e2","text":"hello"})
    assert r.status_code==200 and r.json()["ok"]
    srv.get_engine = orig

def test_snapshot_without_session_404():
    client = TestClient(srv.app)
    r = client.get("/sessions/nonexist/snapshot")
    assert r.status_code==404
