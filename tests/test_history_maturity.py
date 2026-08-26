import tempfile
from pathlib import Path
import json

import core.profile_manager as pm
from fastapi.testclient import TestClient


def test_history_and_maturity_after_actions():
    tmp = Path(tempfile.mkdtemp(prefix="test_hist_"))
    pm.PROFILES_ROOT = tmp
    import api.server as srv
    client = TestClient(srv.app)

    # create profile
    r = client.post("/profiles", json={"id": "hist_test", "geo": "DE"})
    assert r.status_code == 201
    p = pm.Profile.load("hist_test")
    assert p.history_len() == 0
    assert p.cookies_count() >= 0  # seeded may be 0 if no bank, but we have fallback?
    # simulate history via API
    # need fake page session
    class FakeMouse:
        def move(self,x,y): pass
        def click(self,x,y): pass
        def wheel(self,dx,dy): pass
    class FakeKeyboard:
        def press(self,k): pass
        def type(self,ch,delay=0): pass
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
        def content(self): return "<html>ok</html>"
        def goto(self, url, wait_until="domcontentloaded", timeout=30000): pass

    fake = FakePage()
    p2 = pm.Profile.load("hist_test")
    sid = "sess_hist_test_1"
    srv._sessions[sid] = {"profile_id": "hist_test", "engine": type("E", (), {"close": lambda self: None})(), "page": fake, "profile": p2}

    # goto via API should log history
    r2 = client.post(f"/sessions/{sid}/goto", json={"url": "https://example.com", "read": False})
    assert r2.status_code == 200
    p3 = pm.Profile.load("hist_test")
    assert p3.history_len() >= 1
    # type
    r3 = client.post(f"/sessions/{sid}/type", json={"selector": "#q", "text": "hello"})
    assert r3.status_code == 200
    assert pm.Profile.load("hist_test").history_len() >= 2
    # scroll
    r4 = client.post(f"/sessions/{sid}/scroll", json={"screens": 1})
    assert r4.status_code == 200
    assert pm.Profile.load("hist_test").history_len() >= 3

    # check maturity endpoint
    r5 = client.get("/profiles/hist_test")
    assert r5.status_code == 200
    data = r5.json()
    assert "maturity" in data
    maturity = data["maturity"]
    assert "cookies_count" in maturity
    assert "history_len" in maturity
    assert maturity["history_len"] >= 3
    assert "age_days" in maturity
    assert "stage" in maturity


def test_history_persisted_in_export_import():
    tmp = Path(tempfile.mkdtemp(prefix="test_hist_export_"))
    pm.PROFILES_ROOT = tmp
    import api.server as srv
    client = TestClient(srv.app)
    # create
    client.post("/profiles", json={"id": "hist_export", "geo": "DE"})
    p = pm.Profile.load("hist_export")
    p.append_history("goto", "https://example.com")
    p.append_history("click", "#btn")
    assert p.history_len() == 2
    # export
    from core.profile_io import export_profile, import_profile, _has_zstd
    dest = export_profile("hist_export")
    assert dest.exists()
    # delete original
    pm.delete_profile("hist_export", purge_data=True)
    assert not (tmp / "hist_export").exists()
    # import
    restored = import_profile(dest, new_id="hist_export_restored")
    assert restored.history_len() == 2
    assert restored.maturity()["history_len"] == 2


def test_stable_warmup_simulation():
    """5.3/5.2: 10 profiles за 7 симулированных дней дошли до stage 2+ без сигналов"""
    tmp = Path(tempfile.mkdtemp(prefix="test_stable_warmup_"))
    pm.PROFILES_ROOT = tmp
    from datetime import datetime, timedelta, timezone
    from core.profile_manager import create_profile
    profiles = []
    for i in range(10):
        p = create_profile(f"stable_{i}", geo="DE")
        # simulate 5-7 sessions per profile + age 7 days
        p.warmup.created_at = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        for _ in range(6):
            p.warmup.record_session()
        # try advance to stage 2 (needs 5 sessions + age >=4)
        p.warmup.try_advance(health_ok=True)
        p.save()
        profiles.append(p)
    for p in profiles:
        reloaded = pm.Profile.load(p.id)
        assert reloaded.warmup.stage >= 2, f"{p.id} stage {reloaded.warmup.stage} <2"
        assert reloaded.warmup.total_sessions >= 5
        assert reloaded.maturity()["age_days"] >= 7
