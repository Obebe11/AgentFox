"""Тесты Фазы 1.6: locks, cooldown эскалация, warmup advance/regress, sticky proxy."""
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import core.profile_manager as pm

_tmp = tempfile.mkdtemp(prefix="agentfox_t16_")
pm.PROFILES_ROOT = Path(_tmp)

from core.profile_manager import Profile, create_profile
from core.health import Health
from core.warmup import WarmupState
from core.proxy_pool import ProxyConfig


def test_lock_exclusive():
    a = create_profile("lock_a", geo="DE")
    assert a.acquire("owner1"), "first acquire must succeed"
    b = Profile.load("lock_a")
    ok, reason = b.is_locked()
    assert ok and "owner1" in reason, f"second view must see lock: {reason}"
    assert not b.acquire("owner2"), "second acquire must fail"
    a.release()
    c = Profile.load("lock_a")
    assert c.acquire("owner2"), "acquire after release must succeed"
    c.release()


def test_stale_lock_expired():
    p = create_profile("lock_b", geo="DE")
    lock_file = p.dir / ".lock"
    lock_file.write_text("dead-owner", encoding="utf-8")
    # сделать лок старым (2 часа)
    old = time.time() - 7200
    import os

    os.utime(lock_file, (old, old))
    locked, _ = p.is_locked()
    assert not locked, "stale lock (>1h) must be ignored"


def test_cooldown_escalation():
    h = Health()
    h.record_signal("captcha", "https://x.com")
    assert h.status == "cooldown"
    assert h.cooldown_remaining() is not None
    # более серьёзный сигнал не сокращает cooldown
    until_before = h.cooldown_until
    h.record_signal("suspicious", "")
    assert h.status in ("degraded", "banned")

    # серия блокировок → banned
    h2 = Health()
    for _ in range(3):
        h2.record_signal("blocked", "")
    assert h2.status == "banned"


def test_cooldown_expiry():
    from datetime import datetime, timedelta, timezone

    h = Health()
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    h.cooldown_until = past.isoformat()
    assert not h.is_cooldown(), "expired cooldown must be free"


def test_warmup_advance_and_regress():
    w = WarmupState(stage=1)
    w.created_at = __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc
    ).isoformat()
    # стадия 1 → 2 требует age>=4 и sessions>=5
    w.total_sessions = 0
    assert not w.try_advance(health_ok=True), "must not advance without sessions/age"

    # симулируем возраст и сессии
    from datetime import datetime, timedelta, timezone

    w.created_at = (datetime.now(timezone.utc) - timedelta(days=6)).isoformat()
    w.total_sessions = 6
    assert w.try_advance(health_ok=True), "should advance to stage 2"
    assert w.stage == 2
    w.regress()
    assert w.stage == 1, "regress must drop one stage"


def test_warmup_gates_actions():
    w = WarmupState(stage=1)
    allowed = w.allowed_actions()
    assert "browse" in allowed and "extract_deep" not in allowed
    w.stage = 4
    assert w.is_allowed("anything")


def test_sticky_idempotent_and_formats():
    p1 = ProxyConfig(provider="iproyal", server="http://h:1", username="u", password="p")
    p1.apply_sticky("prof_x")
    u_first = p1.username
    p1.apply_sticky("prof_x")
    assert p1.username == u_first, "apply_sticky must be idempotent"
    assert "-session-" in p1.username

    p2 = ProxyConfig(provider="oxylabs", server="http://h:2", username="cust", password="p")
    p2.apply_sticky("prof_y")
    assert "-sesssid-" in p2.username


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
