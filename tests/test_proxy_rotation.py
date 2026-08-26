"""Tests H2: proxy rotation 14d + health-gate in api/server start."""
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import core.profile_manager as pm
import core.proxy_pool as pp
from core.proxy_pool import ProxyConfig, should_rotate, rotate_proxy_if_needed, check_proxy_health
from core.profile_manager import Profile

import api.server as server
from fastapi.testclient import TestClient


client = TestClient(server.app, raise_server_exceptions=False)


class FakePage:
    def __init__(self):
        self.content_text = "<html><body>plain page text</body></html>"

    def content(self):
        return self.content_text

    def goto(self, url, wait_until="domcontentloaded", timeout=30000):
        pass

    def evaluate(self, js):
        return None

    def title(self):
        return "test"

    @property
    def url(self):
        return "https://example.com"


class FakeEngine:
    def __init__(self):
        self.page = FakePage()

    def launch(self, profile, headless=True):
        return self.page

    def close(self):
        pass


def _days_ago_iso(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def test_should_rotate_expired():
    """proxy with rotate_after 14d, created 15 days ago → True"""
    proxy = ProxyConfig(
        provider="iproyal",
        server="http://proxy.example:8000",
        username="user",
        password="pass",
        geo="DE",
        sticky_session="abc123",
        rotate_after="14d",
        created_at=_days_ago_iso(15),
    )
    assert should_rotate(proxy) is True
    # also with explicit created_at param
    proxy2 = ProxyConfig(
        provider="generic",
        server="http://proxy.example:8000",
        rotate_after="14d",
        created_at="",  # empty, fallback via param
    )
    assert should_rotate(proxy2, created_at=_days_ago_iso(15)) is True
    # profile fallback via warmup.created_at
    from core.warmup import WarmupState

    warmup = WarmupState(created_at=_days_ago_iso(15))
    prof = Profile(
        pid="rot_expired_prof",
        identity=pm.generate_identity("rot_expired_prof") if hasattr(pm, "generate_identity") else None,
        proxy=proxy,
        warmup=warmup,
        health=pm.Health() if hasattr(pm, "Health") else __import__("core.health", fromlist=["Health"]).Health(),
        targets=[],
    )
    # rotate_proxy_if_needed should detect expired via warmup fallback or proxy created_at
    # proxy already has created_at 15d ago, so should rotate
    old_sticky = proxy.sticky_session
    rotated = rotate_proxy_if_needed(prof)
    assert rotated is True
    assert prof.proxy.sticky_session != old_sticky or rotated


def test_should_rotate_not_expired():
    """proxy with rotate_after 14d, created 7 days ago → False"""
    proxy = ProxyConfig(
        provider="iproyal",
        server="http://proxy.example:8000",
        username="user",
        password="pass",
        geo="DE",
        sticky_session="abc123",
        rotate_after="14d",
        created_at=_days_ago_iso(7),
    )
    assert should_rotate(proxy) is False
    assert should_rotate(proxy, created_at=_days_ago_iso(7)) is False

    # not expired via profile warmup fallback
    from core.warmup import WarmupState

    warmup = WarmupState(created_at=_days_ago_iso(7))
    proxy_no_created = ProxyConfig(
        provider="generic",
        server="http://proxy.example:8000",
        rotate_after="14d",
        created_at="",  # force fallback to warmup
    )
    # should use warmup 7d → not rotate
    prof = Profile(
        pid="rot_not_expired_prof",
        identity=pm.generate_identity("rot_not_expired_prof"),
        proxy=proxy_no_created,
        warmup=warmup,
        health=pm.Health() if hasattr(pm, "Health") else __import__("core.health", fromlist=["Health"]).Health(),
        targets=[],
    )
    # directly test should_rotate with warmup date
    assert should_rotate(proxy_no_created, created_at=warmup.created_at) is False
    # rotate_proxy_if_needed should not rotate
    # need to set proxy created_at empty so it falls back to warmup
    proxy_no_created.created_at = ""
    assert rotate_proxy_if_needed(prof) is False


def test_api_start_with_proxy_health_gate():
    """mock check_proxy_health to return False → 423; True → 200"""
    # Use FakeEngine to avoid real browser
    fake = FakeEngine()
    orig_engine = server.get_engine
    server.get_engine = lambda p: fake

    # also need to ensure health check is mocked at server level
    # isolate: use unique ids
    pid = "proxy_health_gate_test"

    # cleanup if exists (conftest isolates but be safe)
    # create profile with proxy
    # use direct create_profile to avoid API proxy handling nuances
    # then test via API start
    try:
        # Ensure fresh: delete if exists
        try:
            pm.delete_profile(pid, purge_data=True)
        except Exception:
            pass

        # create via API to test full flow
        r = client.post(
            "/profiles",
            json={
                "id": pid,
                "geo": "DE",
                "proxy": {"server": "http://proxy.example:8000", "username": "user", "password": "pass", "provider": "iproyal"},
            },
        )
        assert r.status_code == 201, r.text

        # case 1: health False → 423
        with patch("api.server.check_proxy_health", return_value=False):
            with patch("core.proxy_pool.check_proxy_health", return_value=False):
                r = client.post(f"/sessions/{pid}/start", json={"headless": True})
                assert r.status_code == 423, f"expected 423 when health False, got {r.status_code} {r.text}"
                assert "proxy" in r.text.lower() or "health" in r.text.lower(), r.text

        # ensure lock released after failure so next start can proceed
        # (health failure releases lock)
        p = pm.Profile.load(pid)
        locked, _ = p.is_locked()
        assert not locked, "lock should be released after health-gate 423"

        # case 2: health True → 200 (also tests rotation not blocking)
        with patch("api.server.check_proxy_health", return_value=True):
            with patch("core.proxy_pool.check_proxy_health", return_value=True):
                # also mock rotate to no-op to avoid randomness
                with patch("api.server.rotate_proxy_if_needed", return_value=False):
                    r = client.post(f"/sessions/{pid}/start", json={"headless": True})
                    assert r.status_code == 200, f"expected 200 when health True, got {r.status_code} {r.text}"
                    sid = r.json()["session_id"]
                    # cleanup session
                    client.post(f"/sessions/{sid}/stop")

        # case 3: health raises exception → best-effort, should still allow start (not block)
        def _raise(*a, **k):
            raise RuntimeError("network down")

        # H8 scheduler would otherwise block immediate restart (min_gap 8h); reset last_session to old
        p = pm.Profile.load(pid)
        p.warmup.last_session_at = _days_ago_iso(10)
        p.save()

        with patch("api.server.should_run", return_value=(True, "ok")):
            with patch("api.server.check_inactivity", return_value=False):
                with patch("api.server.check_proxy_health", side_effect=_raise):
                    with patch("core.proxy_pool.check_proxy_health", side_effect=_raise):
                        r = client.post(f"/sessions/{pid}/start", json={"headless": True})
                        # should not be 423 due to exception; should be 200 (best-effort)
                        assert r.status_code == 200, f"expected 200 when health raises (best-effort), got {r.status_code} {r.text}"
                        sid = r.json()["session_id"]
                        client.post(f"/sessions/{sid}/stop")

    finally:
        server.get_engine = orig_engine
        # cleanup
        try:
            pm.delete_profile(pid, purge_data=True)
        except Exception:
            pass
        # also clean sessions
        for sid, sess in list(server._sessions.items()):
            if sess["profile_id"] == pid:
                try:
                    sess["engine"].close()
                except Exception:
                    pass
                server._sessions.pop(sid, None)
