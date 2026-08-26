import sys, tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import core.profile_manager as pm
pm.PROFILES_ROOT = Path(tempfile.mkdtemp(prefix="agentfox_pr_"))

from core.identity import generate_identity, generate_identity_for_engine
from core.profile_manager import create_profile, switch_profile_engine, Profile
from core.session import get_engine, PatchrightEngine, CamoufoxEngine

def test_identity_engine_deterministic_and_distinct():
    a_ff = generate_identity("same_pid", engine="firefox")
    b_ff = generate_identity("same_pid", engine="firefox")
    a_ch = generate_identity("same_pid", engine="chromium")
    b_ch = generate_identity("same_pid", engine="chromium")
    assert a_ff.fingerprint_preset_id == b_ff.fingerprint_preset_id
    assert a_ch.fingerprint_preset_id == b_ch.fingerprint_preset_id
    assert a_ff.fingerprint_preset_id != a_ch.fingerprint_preset_id
    # canvas seeds also differ
    assert a_ff.canvas_seed != a_ch.canvas_seed

def test_generate_for_engine_wrapper():
    ff = generate_identity_for_engine("pid_x", engine="firefox", geo="DE")
    ch = generate_identity_for_engine("pid_x", engine="chromium", geo="DE")
    assert ff.locale == "de-DE"
    assert ch.locale == "de-DE"
    assert ff.fingerprint_preset_id != ch.fingerprint_preset_id

def test_create_profile_engine_persisted():
    p = create_profile("eng_ff", geo="DE", engine="firefox")
    assert p.engine == "firefox"
    p2 = Profile.load("eng_ff")
    assert p2.engine == "firefox"
    assert p2.identity.fingerprint_preset_id == p.identity.fingerprint_preset_id

def test_switch_engine_regenerates_identity_and_resets_warmup():
    p = create_profile("eng_switch", geo="DE", engine="firefox")
    old_preset = p.identity.fingerprint_preset_id
    old_stage = p.warmup.stage
    p.warmup.stage = 3
    p.warmup.total_sessions = 12
    p.save()
    q = switch_profile_engine("eng_switch", "chromium", reset_warmup=True)
    assert q.engine == "chromium"
    assert q.identity.fingerprint_preset_id != old_preset
    assert q.warmup.stage == 1
    assert q.warmup.total_sessions == 0
    # idempotent switch same engine
    q2 = switch_profile_engine("eng_switch", "chromium")
    assert q2.engine == "chromium"
    assert q2.identity.fingerprint_preset_id == q.identity.fingerprint_preset_id

def test_switch_preserves_proxy_and_id():
    p = create_profile("eng_proxy", geo="DE", proxy={"server": "http://proxy.test:8000", "username": "u", "password": "p"}, engine="firefox")
    orig_proxy_server = p.proxy.server
    q = switch_profile_engine("eng_proxy", "chromium", reset_warmup=False)
    assert q.proxy.server == orig_proxy_server
    assert q.id == "eng_proxy"
    assert q.warmup.stage == 1  # originally 1, preserved when reset_warmup=False? Actually stage 1 stays 1
    # switch back
    r = switch_profile_engine("eng_proxy", "firefox", reset_warmup=False)
    assert r.engine == "firefox"
    # firefox identity should be same as originally created (deterministic)
    orig = generate_identity("eng_proxy", geo="DE", engine="firefox")
    assert r.identity.fingerprint_preset_id == orig.fingerprint_preset_id

def test_get_engine_dispatch():
    p_ff = create_profile("eng_disp_ff", geo="DE", engine="firefox")
    p_ch = create_profile("eng_disp_ch", geo="US", engine="chromium")
    assert isinstance(get_engine(p_ff), CamoufoxEngine)
    assert isinstance(get_engine(p_ch), PatchrightEngine)

def test_api_switch_engine():
    from fastapi.testclient import TestClient
    import api.server as srv
    # isolate PROFILES_ROOT already set via pm, srv uses same pm module (shared)
    client = TestClient(srv.app)
    # create via API
    # ensure profiles root is our temp
    import core.profile_manager as pm2
    # pm already patched, srv imports profile_manager same module
    r = client.post("/profiles", json={"id": "api_eng", "geo": "DE", "engine": "firefox"})
    assert r.status_code == 201, r.text
    r = client.post("/profiles/api_eng/engine", json={"engine": "chromium"})
    assert r.status_code == 200, r.text
    assert r.json()["engine"] == "chromium"
    r = client.post("/profiles/api_eng/engine", json={"engine": "firefox"})
    assert r.json()["engine"] == "firefox"
    r = client.post("/profiles/api_eng/engine", json={"engine": "invalid"})
    assert r.status_code == 400
