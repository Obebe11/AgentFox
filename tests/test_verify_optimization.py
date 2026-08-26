import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
from tools.verify_optimization import run_all

def test_verify_optimization_all_pass():
    report = run_all()
    assert report["summary"]["overall"] == "PASS", report
    assert report["summary"]["failed"] == 0
    assert report["summary"]["total"] == 32
    # каждый домен хотя бы 1 PASS
    for dom, lst in report["by_domain"].items():
        assert any(r["status"] == "PASS" for r in lst), f"{dom} no PASS"

def test_verify_optimization_domains():
    for domain in ["identity", "patches", "scheduler", "profile_io", "metrics", "behavior", "fingerprint", "vps"]:
        rep = run_all(only=domain)
        assert rep["summary"]["failed"] == 0, f"{domain} failed {rep}"
        assert rep["by_domain"][domain]
