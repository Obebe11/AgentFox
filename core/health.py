"""
Health — cooldowns, счётчики фейлов, детект сигналов палева.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

# Сигналы палева — регексы по тексту страницы / URL / заголовкам
SIGNAL_PATTERNS: dict[str, re.Pattern] = {
    "rate_limit": re.compile(r"rate limit|too many requests|429", re.I),
    "captcha": re.compile(r"captcha|arkose|turnstile|challenge|prove you.*human|unusual traffic", re.I),
    "login_wall": re.compile(r"sign in to continue|log in to view|please log in", re.I),
    "blocked": re.compile(r"access denied|blocked by network security|you have been blocked", re.I),
    "logout": re.compile(r"signed out|session expired", re.I),
    "suspicious": re.compile(r"suspicious activity|verify.*identity|unusual activity", re.I),
}

COOLDOWN_BY_SIGNAL: dict[str, timedelta] = {
    "rate_limit": timedelta(hours=6),
    "captcha": timedelta(hours=24),
    "blocked": timedelta(hours=48),
    "logout": timedelta(hours=24),
    "suspicious": timedelta(hours=72),
    "login_wall": timedelta(hours=2),
}


@dataclass
class Health:
    status: str = "ok"  # ok | cooldown | degraded | banned
    consecutive_failures: int = 0
    captcha_events_7d: int = 0
    cooldown_until: Optional[str] = None  # ISO8601
    signals: list[dict] = field(default_factory=list)
    total_sessions: int = 0
    total_extracts: int = 0

    def is_cooldown(self) -> bool:
        if not self.cooldown_until:
            return False
        try:
            until = datetime.fromisoformat(self.cooldown_until)
            return datetime.now(timezone.utc) < until
        except Exception:
            return False

    def cooldown_remaining(self) -> Optional[timedelta]:
        if not self.cooldown_until:
            return None
        try:
            until = datetime.fromisoformat(self.cooldown_until)
            rem = until - datetime.now(timezone.utc)
            return rem if rem.total_seconds() > 0 else None
        except Exception:
            return None

    def record_signal(self, signal: str, url: str = "") -> Optional[timedelta]:
        now = datetime.now(timezone.utc).isoformat()
        self.signals.append({"at": now, "signal": signal, "url": url})
        # keep last 50
        self.signals = self.signals[-50:]
        if signal == "captcha":
            self.captcha_events_7d += 1
        self.consecutive_failures += 1
        # эскалация статуса
        if signal in ("blocked", "suspicious"):
            self.status = "banned" if self.consecutive_failures >= 3 else "degraded"
        elif signal in ("captcha", "rate_limit"):
            self.status = "cooldown"
        # cooldown
        delta = COOLDOWN_BY_SIGNAL.get(signal)
        if delta:
            until = datetime.now(timezone.utc) + delta
            # не уменьшать существующий более длинный cooldown
            if self.cooldown_until:
                try:
                    existing = datetime.fromisoformat(self.cooldown_until)
                    if existing > until:
                        return existing - datetime.now(timezone.utc)
                except Exception:
                    pass
            self.cooldown_until = until.isoformat()
            return delta
        return None

    def record_success(self) -> None:
        self.consecutive_failures = 0
        if self.status == "degraded" and self.consecutive_failures == 0:
            self.status = "ok"
        # cooldown не сбрасываем автоматически — только по времени

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "consecutive_failures": self.consecutive_failures,
            "captcha_events_7d": self.captcha_events_7d,
            "cooldown_until": self.cooldown_until,
            "signals": self.signals[-10:],
            "total_sessions": self.total_sessions,
            "total_extracts": self.total_extracts,
        }


def detect_signals(text: str, url: str = "") -> list[str]:
    hits: list[str] = []
    haystack = f"{text}\n{url}"
    for name, pat in SIGNAL_PATTERNS.items():
        if pat.search(haystack):
            hits.append(name)
    return hits
