"""
Scheduler — джиттер расписания + часы в таймзоне профиля (§4, §7.3).

Принципы:
- cron не ровный, а `интервал ± случайный сдвиг` (spread 0.4 по умолчанию)
- часы активности в таймзоне профиля (09–23 локального времени, конфигурируемо)
- стадия warmup влияет на интервал (молодой профиль — реже)
- простой >N дней → регрессия стадии (человек после отпуска)

Всё детерминировано от profile_id + времени, но с джиттером — связка профилей невозможна.
"""
from __future__ import annotations

import hashlib
import random
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from .profile_manager import Profile
from .warmup import STAGES

# Базовый интервал по стадии (минимальный gap между сессиями)
BASE_INTERVAL_BY_STAGE: dict[int, timedelta] = {
    1: timedelta(hours=18),   # 1–2 сессии/день ≈ 12–36ч с джиттером
    2: timedelta(hours=12),
    3: timedelta(hours=8),
    4: timedelta(hours=6),
}

# Активное окно по умолчанию (локальное время профиля)
DEFAULT_ACTIVE_START = 9   # 09:00
DEFAULT_ACTIVE_END = 23    # 23:00

# Простой >N дней → регрессия
INACTIVITY_THRESHOLD_DAYS = 7


def _rng_for(profile_id: str, salt: str = "") -> random.Random:
    h = hashlib.sha256(f"{profile_id}:{salt}".encode()).hexdigest()
    return random.Random(int(h[:16], 16))


def jittered_interval(base_seconds: float, spread: float = 0.4, rng: Optional[random.Random] = None) -> float:
    """Гауссов джиттер: base * (1 ± spread*rand). Минимум 60с."""
    r = rng or random
    # используем gauss если есть, иначе uniform fallback
    try:
        factor = r.gauss(1.0, spread)
    except Exception:
        factor = r.uniform(1 - spread, 1 + spread)
    val = base_seconds * factor
    return max(60.0, val)


def next_run_after(
    last_run: datetime,
    base_interval: timedelta,
    spread: float = 0.4,
    rng: Optional[random.Random] = None,
) -> datetime:
    """Следующий запуск = last_run + jittered(base_interval)."""
    jitter = jittered_interval(base_interval.total_seconds(), spread=spread, rng=rng)
    return last_run + timedelta(seconds=jitter)


def _to_profile_tz(dt: datetime, tz_name: str) -> datetime:
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz)


def is_in_active_window(dt: datetime, tz_name: str, start_hour: int = DEFAULT_ACTIVE_START, end_hour: int = DEFAULT_ACTIVE_END) -> bool:
    """Активен ли час в таймзоне профиля? 09–23 по умолчанию."""
    local = _to_profile_tz(dt, tz_name)
    h = local.hour
    if start_hour <= end_hour:
        return start_hour <= h < end_hour
    # окно через полночь (например 22–06)
    return h >= start_hour or h < end_hour


def next_active_time(dt: datetime, tz_name: str, start_hour: int = DEFAULT_ACTIVE_START, end_hour: int = DEFAULT_ACTIVE_END) -> datetime:
    """Если dt вне активного окна — сдвигает к началу следующего окна."""
    if is_in_active_window(dt, tz_name, start_hour, end_hour):
        return dt
    local = _to_profile_tz(dt, tz_name)
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc
    # сдвигаем к start_hour следующего дня (или сегодня если ещё до окна)
    if local.hour < start_hour:
        # сегодня в start_hour
        candidate = local.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    else:
        # завтра в start_hour
        candidate = (local + timedelta(days=1)).replace(hour=start_hour, minute=0, second=0, microsecond=0)
    return candidate.astimezone(timezone.utc)


def schedule_next(
    profile: Profile,
    now: Optional[datetime] = None,
    spread: float = 0.4,
    start_hour: int = DEFAULT_ACTIVE_START,
    end_hour: int = DEFAULT_ACTIVE_END,
) -> datetime:
    """Следующий запуск для профиля с учётом стадии, джиттера и таймзоны."""
    if now is None:
        now = datetime.now(timezone.utc)
    stage = profile.warmup.stage
    base = BASE_INTERVAL_BY_STAGE.get(stage, BASE_INTERVAL_BY_STAGE[4])
    # детерминированный rng от profile_id + last_session
    salt = profile.warmup.last_session_at or profile.warmup.created_at or now.isoformat()
    rng = _rng_for(profile.id, salt)
    # джиттер
    nxt = next_run_after(now, base, spread=spread, rng=rng)
    # сдвиг в активное окно
    nxt = next_active_time(nxt, profile.identity.timezone, start_hour, end_hour)
    return nxt


def should_run(profile: Profile, now: Optional[datetime] = None) -> tuple[bool, str]:
    """Можно ли запускать профиль сейчас? Проверяет locks/cooldown/active_hours/интервал."""
    if now is None:
        now = datetime.now(timezone.utc)
    locked, reason = profile.is_locked()
    if locked:
        return False, f"locked: {reason}"
    if not profile.warmup.is_allowed("browse"):
        return False, f"warmup stage {profile.warmup.stage} — browsing not allowed"
    if not is_in_active_window(now, profile.identity.timezone):
        return False, f"outside active window {DEFAULT_ACTIVE_START}-{DEFAULT_ACTIVE_END} {profile.identity.timezone}"
    # проверяем что прошло достаточно времени с last_session
    if profile.warmup.last_session_at:
        try:
            last = datetime.fromisoformat(profile.warmup.last_session_at)
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            # минимальный интервал без джиттера — половина базы (чтобы не спамить)
            base = BASE_INTERVAL_BY_STAGE.get(profile.warmup.stage, BASE_INTERVAL_BY_STAGE[4])
            min_gap = base.total_seconds() * 0.45
            if (now - last).total_seconds() < min_gap:
                return False, f"too soon: {int((now-last).total_seconds())}s < {int(min_gap)}s min gap"
        except Exception:
            pass
    return True, "ok"


def check_inactivity(profile: Profile, now: Optional[datetime] = None) -> bool:
    """Если простой >7 дней — регрессирует warmup на 1 (человек после отпуска). Возвращает True если регрессировал."""
    if now is None:
        now = datetime.now(timezone.utc)
    if not profile.warmup.last_session_at:
        return False
    try:
        last = datetime.fromisoformat(profile.warmup.last_session_at)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        delta = now - last
        if delta.days >= INACTIVITY_THRESHOLD_DAYS:
            before = profile.warmup.stage
            profile.warmup.regress()
            if profile.warmup.stage != before:
                profile.save()
                return True
    except Exception:
        pass
    return False
