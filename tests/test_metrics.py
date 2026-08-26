import sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import core.profile_manager as pm
pm.PROFILES_ROOT = Path(tempfile.mkdtemp(prefix="agentfox_metr_"))
from core.metrics import init_db, record_event, get_success_rate, get_per_target, get_per_profile, get_health_series, get_overall_stats, get_recent_events, clear_all

def test_record_and_rates():
    clear_all()
    init_db()
    record_event("p1", target="x.com", event_type="extract", success=True)
    record_event("p1", target="x.com", event_type="extract", success=False, error="captcha")
    record_event("p1", target="y.com", event_type="goto", success=True)
    record_event("p2", target="x.com", event_type="extract", success=True)

    r = get_success_rate(profile_id="p1", days=1)
    assert r["total"] == 3
    assert r["success"] == 2
    assert r["fail"] == 1
    assert 0.66 < r["rate"] < 0.67

    r2 = get_success_rate(target="x.com", days=1)
    assert r2["total"] == 3  # p1x2 + p2
    assert r2["success"] == 2

    g = get_overall_stats(days=1)
    assert g["total"] == 4

def test_per_target_and_profile():
    clear_all()
    for i in range(5):
        record_event("a", target="x.com", success=True)
    for i in range(2):
        record_event("a", target="y.com", success=False)
    record_event("b", target="x.com", success=True)
    per_t = get_per_target(days=1)
    # x.com should be top
    assert per_t[0]["target"] == "x.com"
    assert per_t[0]["total"] == 6
    per_p = get_per_profile(days=1)
    assert per_p[0]["profile_id"] == "a"
    assert per_p[0]["total"] == 7

def test_health_series_and_recent():
    clear_all()
    record_event("p1", target="x.com", success=True)
    record_event("p1", target="x.com", success=False)
    series = get_health_series("p1", days=1)
    assert len(series) == 1
    assert series[0]["total"] == 2
    ev = get_recent_events(limit=5, profile_id="p1")
    assert len(ev) == 2
    assert ev[0]["profile_id"] == "p1"

def test_api_metrics_endpoints():
    from fastapi.testclient import TestClient
    import api.server as srv
    from core.profile_manager import create_profile
    clear_all()
    # ensure profile exists for /metrics/{pid} which checks Profile.load
    try:
        create_profile("p1", geo="DE")
    except FileExistsError:
        pass
    record_event("p1", target="example.com", success=True)
    record_event("p1", target="example.com", success=False)
    client = TestClient(srv.app)
    r = client.get("/metrics")
    assert r.status_code == 200, r.text
    assert "total" in r.json() or "overall" in r.json()
    r = client.get("/metrics/p1")
    assert r.status_code == 200
    assert r.json()["profile_id"] == "p1"
