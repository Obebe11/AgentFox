import sys, tempfile
from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent.parent))

import core.profile_manager as pm
pm.PROFILES_ROOT = Path(tempfile.mkdtemp(prefix="agentfox_sched_"))

from core.scheduler import (
    jittered_interval,
    next_run_after,
    is_in_active_window,
    next_active_time,
    schedule_next,
    should_run,
    check_inactivity,
    BASE_INTERVAL_BY_STAGE,
)
from core.profile_manager import create_profile

def test_jitter_range():
    import random
    rng = random.Random(0)
    base = 3600
    vals = [jittered_interval(base, spread=0.4, rng=rng) for _ in range(100)]
    # все в пределах разумного (60s .. base*2)
    assert all(v >= 60 for v in vals)
    assert min(vals) < base < max(vals)
    # среднее около base
    avg = sum(vals)/len(vals)
    assert 0.7*base < avg < 1.3*base

def test_next_run_after():
    import random
    rng = random.Random(42)
    last = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    nxt = next_run_after(last, timedelta(hours=6), spread=0.4, rng=rng)
    delta = (nxt - last).total_seconds()
    assert 60 < delta < 6*3600*2

def test_active_window():
    # Europe/Berlin = UTC+1 в январе
    dt_inside = datetime(2026, 1, 1, 10, 0, tzinfo=ZoneInfo("Europe/Berlin"))  # 10 Berlin
    dt_outside = datetime(2026, 1, 1, 2, 0, tzinfo=ZoneInfo("Europe/Berlin"))  # 02 Berlin
    # конвертируем в UTC для проверки
    dt_inside_utc = dt_inside.astimezone(timezone.utc)
    dt_outside_utc = dt_outside.astimezone(timezone.utc)
    assert is_in_active_window(dt_inside_utc, "Europe/Berlin") is True
    assert is_in_active_window(dt_outside_utc, "Europe/Berlin") is False

def test_next_active_time_shifts():
    # 02 Berlin (01 UTC) вне окна 09-23 -> должен сдвинуть к 09 Berlin = 08 UTC
    dt = datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc)  # 02 Berlin
    nxt = next_active_time(dt, "Europe/Berlin")
    local = nxt.astimezone(ZoneInfo("Europe/Berlin"))
    assert local.hour == 9
    assert local.minute == 0

def test_schedule_next_deterministic():
    p = create_profile("sched_1", geo="DE")
    # 11 Berlin = 10 UTC внутри окна 09-23 — избегаем снапа к 09:00
    now = datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc)
    a = schedule_next(p, now=now)
    b = schedule_next(p, now=now)
    assert a == b  # детерминирован при одинаковом now
    # другой профиль — другое время (джиттер разный) — проверяем на уровне jitter интервала
    from core.scheduler import jittered_interval, _rng_for
    rng1 = _rng_for("sched_1", p.warmup.last_session_at or p.warmup.created_at)
    rng2 = _rng_for("sched_2", "different_salt")
    v1 = jittered_interval(6*3600, spread=0.4, rng=rng1)
    v2 = jittered_interval(6*3600, spread=0.4, rng=rng2)
    assert v1 != v2

def test_should_run_guards():
    p = create_profile("sched_guard", geo="DE")
    # вне активного окна — block
    night = datetime(2026, 1, 1, 2, 0, tzinfo=ZoneInfo("Europe/Berlin")).astimezone(timezone.utc)
    # но is_in_active_window проверяет Berlin час -> night is 03 Berlin? Actually 02 UTC = 03 Berlin still outside
    ok, reason = should_run(p, now=night)
    assert ok is False and "active window" in reason
    # внутри окна — ok
    day = datetime(2026, 1, 1, 11, 0, tzinfo=ZoneInfo("Europe/Berlin")).astimezone(timezone.utc)
    ok, _ = should_run(p, now=day)
    assert ok is True

def test_inactivity_regress():
    from datetime import datetime, timezone
    import time
    p = create_profile("sched_inact", geo="DE")
    p.warmup.stage = 3
    p.warmup.last_session_at = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    p.save()
    regressed = check_inactivity(p)
    assert regressed is True
    assert p.warmup.stage == 2
    # повтор без сохранения не регрессирует снова если уже проверено недавно? Но теперь last_session всё ещё 8 дней назад, но stage 2 -> может ещё регрессировать при повторном вызове
    # Проверим что при свежей активности не регрессирует
    p2 = create_profile("sched_inact2", geo="DE")
    p2.warmup.stage = 2
    p2.warmup.last_session_at = datetime.now(timezone.utc).isoformat()
    p2.save()
    assert check_inactivity(p2) is False
