"""
Warmup — автомат прогрева. Стадии гейтят действия.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

STAGES = {
    1: {"days": (1, 3), "min_sessions": 0, "allowed": ["browse", "read"], "session_min": "5-10m"},
    2: {"days": (4, 7), "min_sessions": 5, "allowed": ["browse", "read", "search", "extract_light"], "session_min": "10-20m"},
    3: {"days": (8, 14), "min_sessions": 10, "allowed": ["browse", "read", "search", "extract_light", "extract_deep", "navigate"], "session_min": "20-40m"},
    4: {"days": (15, 999), "min_sessions": 16, "allowed": ["*"], "session_min": "5-40m"},
}


@dataclass
class WarmupState:
    stage: int = 1
    total_sessions: int = 0
    created_at: str = ""  # ISO8601
    last_session_at: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()

    def allowed_actions(self) -> list[str]:
        cfg = STAGES.get(self.stage, STAGES[4])
        return cfg["allowed"]

    def is_allowed(self, action: str) -> bool:
        allowed = self.allowed_actions()
        return "*" in allowed or action in allowed

    def age_days(self) -> int:
        try:
            created = datetime.fromisoformat(self.created_at)
            return (datetime.now(timezone.utc) - created).days + 1
        except Exception:
            return 1

    def try_advance(self, health_ok: bool = True) -> bool:
        """Пытается повысить стадию. Возвращает True если повысил."""
        if not health_ok:
            return False
        age = self.age_days()
        nxt = self.stage + 1
        if nxt not in STAGES:
            return False
        cfg = STAGES[nxt]
        if age >= cfg["days"][0] and self.total_sessions >= cfg["min_sessions"]:
            self.stage = nxt
            return True
        return False

    def regress(self) -> None:
        if self.stage > 1:
            self.stage -= 1

    def record_session(self) -> None:
        self.total_sessions += 1
        self.last_session_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict:
        cfg = STAGES.get(self.stage, STAGES[4])
        return {
            "stage": self.stage,
            "total_sessions": self.total_sessions,
            "age_days": self.age_days(),
            "allowed_actions": cfg["allowed"],
            "session_limit": cfg["session_min"],
            "created_at": self.created_at,
            "last_session_at": self.last_session_at,
        }
