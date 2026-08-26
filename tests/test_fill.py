import tempfile
from pathlib import Path

import core.profile_manager as pm
from fastapi.testclient import TestClient


def _setup_isolated(tmp_root: Path):
    pm.PROFILES_ROOT = tmp_root
    import api.server as srv
    from api.server import app

    client = TestClient(app)
    return client, srv


def test_fill_alias_works_for_input_and_contenteditable():
    tmp = Path(tempfile.mkdtemp(prefix="test_fill_alias_"))
    client, srv = _setup_isolated(tmp)
    r = client.post("/profiles", json={"id": "fill_input", "geo": "DE"})
    assert r.status_code == 201

    class FakeMouse:
        def move(self, x, y): pass
        def click(self, x, y): pass
        def wheel(self, dx, dy): pass

    class FakeKeyboard:
        def __init__(self):
            self.events = []
        def press(self, k): self.events.append(k)
        def type(self, ch, delay=0): self.events.append((ch, delay))

    class FakeLocator:
        @property
        def first(self): return self
        def click(self, timeout=5000): pass
        def bounding_box(self, timeout=10000): return {"x": 0, "y": 0, "width": 10, "height": 10}

    class FakePage:
        def __init__(self):
            self.mouse = FakeMouse()
            self.keyboard = FakeKeyboard()
        def evaluate(self, js):
            if "innerWidth" in js: return {"x": 640, "y": 360}
            return None
        def locator(self, s): return FakeLocator()
        def fill(self, s, t): pass
        def content(self): return "<html>ok</html>"

    fake = FakePage()
    from core.profile_manager import Profile
    p = Profile.load("fill_input")
    sid = "sess_fill_input_1"
    srv._sessions[sid] = {"profile_id": "fill_input", "engine": type("E", (), {"close": lambda self: None})(), "page": fake, "profile": p}

    # ordinary input via /type still works
    r2 = client.post(f"/sessions/{sid}/type", json={"selector": "#q", "text": "hello"})
    assert r2.status_code == 200

    # contenteditable via /fill
    fake2 = FakePage()
    srv._sessions[sid]["page"] = fake2
    r3 = client.post(f"/sessions/{sid}/fill", json={"selector": '[contenteditable="true"]', "text": "rich editor text"})
    assert r3.status_code == 200
    # keyboard events: press stores string, type stores (ch, delay)
    delays = [delay for ev in fake2.keyboard.events if isinstance(ev, tuple) for _, delay in [ev]]
    assert len(delays) == len("rich editor text")
    assert 45 <= min(delays) <= max(delays) <= 180


def test_fill_with_e_ref():
    tmp = Path(tempfile.mkdtemp(prefix="test_fill_e_"))
    client, srv = _setup_isolated(tmp)
    client.post("/profiles", json={"id": "fill_e", "geo": "DE"})

    class FakeMouse:
        def move(self, x, y): pass
        def click(self, x, y): pass
        def wheel(self, dx, dy): pass
    class FakeKeyboard:
        def __init__(self): self.events=[]
        def press(self,k): self.events.append(k)
        def type(self,ch,delay=0): self.events.append(ch)
    class FakeLocator:
        @property
        def first(self): return self
        def click(self,timeout=5000): pass
        def bounding_box(self,timeout=10000): return {"x":0,"y":0,"width":10,"height":10}
    class FakePage:
        def __init__(self):
            self.mouse=FakeMouse(); self.keyboard=FakeKeyboard()
        def evaluate(self,js): 
            if "innerWidth" in js: return {"x":640,"y":360}
            return None
        def locator(self,s): return FakeLocator()
        def fill(self,s,t): pass
        def content(self): return "<div contenteditable='true'>ok</div>"

    fake = FakePage()
    from core.profile_manager import Profile
    p = Profile.load("fill_e")
    sid = "sess_fill_e_1"
    srv._sessions[sid] = {"profile_id": "fill_e", "engine": type("E", (), {"close": lambda self: None})(), "page": fake, "profile": p, "snapshot_refs": {"@e1": '[contenteditable="true"]'}}
    r = client.post(f"/sessions/{sid}/fill", json={"selector": "@e1", "text": "from snapshot"})
    assert r.status_code == 200
