"""
team — Team / RBAC + логи + 2FA/SOC2 (P1, ADS Power sharing).

Минимальный стек для AgentFox:
- роли: admin / member / viewer
- API-ключи per-agent (hash at-rest, prefix visible)
- изоляция по targets: профиль видит только владелец или admin
- аудит-лог: все действия в SQLite (аналогично metrics)
- 2FA: TOTP secret per-user (опционально)

Хранение: profiles/team.json (users) + profiles/audit.db (лог)
Интеграция: api/server.py проверяет заголовок X-AgentFox-Key (опционально, если team.json есть)
Если team.json отсутствует — single-tenant режим (обратно совместимо).
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .profile_manager import PROFILES_ROOT

TEAM_JSON = PROFILES_ROOT / "team.json"
AUDIT_DB = PROFILES_ROOT / "audit.db"

ROLES = ("admin", "member", "viewer")
ROLE_PERMS = {
    "admin": {"profiles": "rw", "sessions": "rw", "team": "rw", "metrics": "rw", "export": "rw"},
    "member": {"profiles": "rw", "sessions": "rw", "team": "r", "metrics": "rw", "export": "rw"},
    "viewer": {"profiles": "r", "sessions": "r", "team": "r", "metrics": "r", "export": "r"},
}


@dataclass
class TeamMember:
    id: str
    name: str
    role: str = "member"
    api_key_prefix: str = ""
    api_key_hash: str = ""
    totp_secret: Optional[str] = None
    created_at: str = ""
    targets: list[str] | None = None  # ограничение по targets если задано
    enabled: bool = True

    def to_dict(self, redact: bool = True) -> dict:
        d = asdict(self)
        if redact:
            d.pop("api_key_hash", None)
            d.pop("totp_secret", None)
        return d


def _team_path() -> Path:
    from .profile_manager import PROFILES_ROOT as PR

    return PR / "team.json"


def _audit_db_path() -> Path:
    from .profile_manager import PROFILES_ROOT as PR

    return PR / "audit.db"


def _load_team() -> dict:
    p = _team_path()
    if not p.exists():
        return {"members": [], "created_at": datetime.now(timezone.utc).isoformat()}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"members": [], "created_at": datetime.now(timezone.utc).isoformat()}


def _save_team(data: dict) -> None:
    p = _team_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def list_members() -> list[dict]:
    data = _load_team()
    return data.get("members", [])


def get_member(member_id: str) -> Optional[dict]:
    for m in list_members():
        if m.get("id") == member_id:
            return m
    return None


def get_member_by_prefix(prefix: str) -> Optional[dict]:
    for m in list_members():
        if m.get("api_key_prefix") == prefix:
            return m
    return None


def authenticate(api_key: str) -> Optional[dict]:
    """Проверяет API ключ, возвращает member dict или None."""
    if not api_key or len(api_key) < 8:
        return None
    prefix = api_key[:8]
    m = get_member_by_prefix(prefix)
    if not m or not m.get("enabled", True):
        return None
    if _hash_key(api_key) != m.get("api_key_hash"):
        return None
    return m


def create_member(name: str, role: str = "member", targets: list[str] | None = None) -> dict:
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    data = _load_team()
    mid = f"u_{secrets.token_hex(4)}"
    key = f"afk_{secrets.token_urlsafe(24)}"
    prefix = key[:8]
    member = TeamMember(
        id=mid,
        name=name,
        role=role,
        api_key_prefix=prefix,
        api_key_hash=_hash_key(key),
        created_at=datetime.now(timezone.utc).isoformat(),
        targets=targets or [],
        enabled=True,
    )
    d = asdict(member)
    data.setdefault("members", []).append(d)
    _save_team(data)
    # вернуть с ключом один раз (не храним plain)
    out = member.to_dict(redact=False)
    out["api_key"] = key
    out.pop("api_key_hash", None)
    # audit
    try:
        audit_log(mid, "team.create_member", targets[0] if targets else "", {"role": role})
    except Exception:
        pass
    return out


def delete_member(member_id: str) -> bool:
    data = _load_team()
    before = len(data.get("members", []))
    data["members"] = [m for m in data.get("members", []) if m.get("id") != member_id]
    if len(data["members"]) == before:
        return False
    _save_team(data)
    try:
        audit_log("system", "team.delete_member", member_id, {})
    except Exception:
        pass
    return True


def set_member_role(member_id: str, role: str) -> dict:
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    data = _load_team()
    for m in data.get("members", []):
        if m.get("id") == member_id:
            m["role"] = role
            _save_team(data)
            return m
    raise FileNotFoundError(f"member {member_id} not found")


def rotate_api_key(member_id: str) -> dict:
    data = _load_team()
    for m in data.get("members", []):
        if m.get("id") == member_id:
            key = f"afk_{secrets.token_urlsafe(24)}"
            m["api_key_prefix"] = key[:8]
            m["api_key_hash"] = _hash_key(key)
            _save_team(data)
            out = dict(m)
            out["api_key"] = key
            out.pop("api_key_hash", None)
            return out
    raise FileNotFoundError(f"member {member_id} not found")


def check_permission(member: dict | None, resource: str, action: str = "r") -> bool:
    """Проверка прав. Если team.json пуст — разрешено всё (single-tenant)."""
    if not _team_path().exists():
        return True
    if member is None:
        return False
    role = member.get("role", "viewer")
    perms = ROLE_PERMS.get(role, {})
    level = perms.get(resource, "r")
    if action == "r":
        return level in ("r", "rw")
    if action in ("w", "rw"):
        return level == "rw"
    return False


def is_team_enabled() -> bool:
    return _team_path().exists()


# --- 2FA TOTP helpers ---

def setup_totp(member_id: str) -> dict:
    """Генерирует TOTP secret для member."""
    try:
        import secrets as _s
        import base64

        raw = _s.token_bytes(20)
        secret = base64.b32encode(raw).decode().rstrip("=")
        data = _load_team()
        for m in data.get("members", []):
            if m.get("id") == member_id:
                m["totp_secret"] = secret
                _save_team(data)
                return {"member_id": member_id, "secret": secret, "otpauth_url": f"otpauth://totp/AgentFox:{member_id}?secret={secret}&issuer=AgentFox"}
        raise FileNotFoundError(f"member {member_id} not found")
    except Exception as e:
        raise RuntimeError(str(e))


def verify_totp(member_id: str, code: str) -> bool:
    """Проверяет TOTP код (6 цифр)."""
    m = get_member(member_id)
    if not m or not m.get("totp_secret"):
        return False
    secret = m["totp_secret"]
    try:
        import base64
        import hmac
        import hashlib as _h
        import struct
        import time as _t

        key = base64.b32decode(secret + "=" * (-len(secret) % 8))
        for offset in (-1, 0, 1):
            counter = int(_t.time() // 30) + offset
            msg = struct.pack(">Q", counter)
            digest = hmac.new(key, msg, _h.sha1).digest()
            o = digest[19] & 0xF
            token = (struct.unpack(">I", digest[o : o + 4])[0] & 0x7FFFFFFF) % 1000000
            if f"{token:06d}" == code:
                return True
        return False
    except Exception:
        return False


# --- Audit log (SQLite) ---

def _audit_connect() -> sqlite3.Connection:
    p = _audit_db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p), check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def _init_audit() -> None:
    con = _audit_connect()
    try:
        con.execute("""
        CREATE TABLE IF NOT EXISTS audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            actor TEXT NOT NULL,
            action TEXT NOT NULL,
            target TEXT,
            detail TEXT
        )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit(actor)")
        con.commit()
    finally:
        con.close()


def audit_log(actor: str, action: str, target: str = "", detail: dict | None = None) -> int:
    _init_audit()
    con = _audit_connect()
    try:
        ts = datetime.now(timezone.utc).isoformat()
        cur = con.execute(
            "INSERT INTO audit (ts, actor, action, target, detail) VALUES (?,?,?,?,?)",
            (ts, actor, action, target, json.dumps(detail or {}, ensure_ascii=False)),
        )
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def get_audit(limit: int = 50, actor: str | None = None) -> list[dict]:
    _init_audit()
    con = _audit_connect()
    try:
        if actor:
            rows = con.execute("SELECT * FROM audit WHERE actor=? ORDER BY id DESC LIMIT ?", (actor, limit)).fetchall()
        else:
            rows = con.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def clear_audit() -> None:
    _init_audit()
    con = _audit_connect()
    try:
        con.execute("DELETE FROM audit")
        con.commit()
    finally:
        con.close()
