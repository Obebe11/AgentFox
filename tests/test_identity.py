from core.identity import generate_identity
from core.profile_manager import create_profile, list_profiles, Profile
from core.health import detect_signals
from core.warmup import WarmupState
import tempfile, pathlib

def test_identity_stable():
    a = generate_identity("prof_test_1")
    b = generate_identity("prof_test_1")
    assert a.canvas_seed == b.canvas_seed
    assert a.webgl_seed == b.webgl_seed
    c = generate_identity("prof_test_2")
    assert a.canvas_seed != c.canvas_seed

def test_geo_locale():
    p = generate_identity("x", geo="DE")
    assert p.locale == "de-DE"
    assert p.timezone == "Europe/Berlin"

def test_warmup_gate():
    w = WarmupState(stage=1)
    assert w.is_allowed("browse")
    assert not w.is_allowed("extract_light")
    w.stage = 2
    assert w.is_allowed("extract_light")

def test_detect_signals():
    assert "captcha" in detect_signals("Please complete captcha challenge", "https://x.com")
    assert "rate_limit" in detect_signals("429 Too Many Requests", "")
    assert detect_signals("normal page content", "https://example.com") == []

def test_profile_create(tmp_path=None):
    # изоляция — временно подменяем PROFILES_ROOT
    import core.profile_manager as pm
    orig = pm.PROFILES_ROOT
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        pm.PROFILES_ROOT = pathlib.Path(td)
        p = create_profile("testprof", geo="DE", targets=["x.com"])
        assert p.identity.locale == "de-DE"
        loaded = Profile.load("testprof")
        assert loaded.id == "testprof"
        assert loaded.warmup.stage == 1
        pm.PROFILES_ROOT = orig
