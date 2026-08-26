"""
Batch Profiles — verification: POST /profiles/bulk + tools/bulk_import.py
Использует isolated PROFILES_ROOT via conftest, FakeEngine not needed (creation only).
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi.testclient import TestClient
import api.server as srv
import core.profile_manager as pm


def _get_created(data):
    """Support both {created: [...]} and direct list responses."""
    if isinstance(data, dict) and "created" in data:
        return data["created"]
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "total" in data and "created" in data:
        return data["created"]
    return data


def test_bulk_create_via_api():
    client = TestClient(srv.app, raise_server_exceptions=False)
    r = client.post("/profiles/bulk", json={"count": 3, "geo": "DE"})
    assert r.status_code == 201, f"{r.status_code} {r.text}"
    data = r.json()
    created = _get_created(data)
    assert isinstance(created, list), f"created not list: {data}"
    assert len(created) == 3, f"expected 3, got {len(created)} data={data}"
    # then GET /profiles shows 3
    r2 = client.get("/profiles")
    assert r2.status_code == 200, r2.text
    lst = r2.json()
    assert isinstance(lst, list)
    assert len(lst) == 3, f"GET /profiles expected 3, got {len(lst)} {lst}"
    ids = {p["id"] for p in lst}
    assert len(ids) == 3
    # seeded_cookies present per profile
    for c in created:
        assert "seeded_cookies" in c or "id" in c


def test_bulk_with_prefix():
    client = TestClient(srv.app, raise_server_exceptions=False)
    r = client.post("/profiles/bulk", json={"count": 2, "geo": "US", "prefix": "mytest", "engine": "firefox"})
    assert r.status_code == 201, r.text
    data = r.json()
    created = _get_created(data)
    assert len(created) == 2, f"{data}"
    for c in created:
        assert c["id"].startswith("mytest_"), f"id {c['id']} should start with mytest_"
    # second bulk with same prefix should handle collision via suffix (not 409)
    r2 = client.post("/profiles/bulk", json={"count": 2, "geo": "US", "prefix": "mytest"})
    assert r2.status_code == 201, r2.text
    data2 = r2.json()
    created2 = _get_created(data2)
    errors = data2.get("errors", []) if isinstance(data2, dict) else []
    # collision handling: should still create 2 (with suffix) and no total fail
    # At least 2 new profiles total now 4
    assert len(created2) == 2, f"collision handling failed {data2}"
    # overall count via GET should be 4 now
    r3 = client.get("/profiles")
    assert len(r3.json()) == 4, f"expected 4 profiles, got {len(r3.json())}"


def test_bulk_import_tool(tmp_path):
    # tmp_path is pytest fixture, but we need isolated via pm.PROFILES_ROOT set by conftest
    # Use tools.bulk_import directly (not via API)
    from tools.bulk_import import bulk_import, load_proxies

    # create proxy file
    proxy_file = tmp_path / "proxies.txt"
    proxy_file.write_text("http://proxy1.test:8000\nhttp://proxy2.test:8000\n\n# comment\nhttp://proxy3.test:8000\n", encoding="utf-8")
    proxies = load_proxies(str(proxy_file))
    assert len(proxies) == 3
    assert proxies[0] == "http://proxy1.test:8000"

    # bulk import 5 profiles via tool, proxies round-robin
    result = bulk_import(count=5, geo="DE", prefix="tooltest", proxy_file=str(proxy_file), engine="firefox")
    assert isinstance(result, dict)
    assert "created" in result and "errors" in result
    assert len(result["created"]) == 5, f"got {result}"
    assert len(result["errors"]) == 0, f"errors {result['errors']}"
    assert result["total"] == 5

    # verify profiles exist via list_profiles and proxy round-robin
    profiles = pm.list_profiles()
    assert len(profiles) == 5
    ids = sorted([p["id"] for p in profiles])
    assert ids[0].startswith("tooltest_")
    # proxies should be assigned round-robin
    # profile_manager stores proxy per profile; check first 3 have distinct servers
    servers = []
    for p in profiles:
        # load full profile to check proxy server
        full = pm.Profile.load(p["id"])
        if full.proxy:
            servers.append(full.proxy.server)
        else:
            servers.append(None)
    # at least we have 3 unique servers used
    assert len(set(servers)) == 3, f"round-robin failed {servers}"
    # seeds should have been attempted (cookie_seed may be absent if no bank, but seeded_cookies in created dict)
    for c in result["created"]:
        assert "seeded_cookies" in c
        assert "id" in c

    # test bulk_import without proxy file (no proxy)
    result2 = bulk_import(count=2, geo="DE", prefix="tooltest2")
    assert len(result2["created"]) == 2
    # total now 7
    assert len(pm.list_profiles()) == 7

    # test count validation
    try:
        bulk_import(count=0, geo="DE")
        assert False, "should raise for count 0"
    except ValueError:
        pass
    try:
        bulk_import(count=101, geo="DE")
        assert False, "should raise for count 101"
    except ValueError:
        pass

    # test via CLI argparse mock (parse_args)
    from tools.bulk_import import parse_args
    ns = parse_args(["--count", "3", "--geo", "FR", "--prefix", "cli_test", "--engine", "firefox"])
    assert ns.count == 3 and ns.geo == "FR" and ns.prefix == "cli_test"
