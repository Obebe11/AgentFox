"""
Metrics — success-rate per target, график здоровья (SQLite + endpoint /metrics).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from .profile_manager import PROFILES_ROOT

def _db_path() -> Path:
    # динамический — уважает monkeypatched PROFILES_ROOT в тестах
    from .profile_manager import PROFILES_ROOT as PR
    return PR / "metrics.db"

_DB_INITED: set[str] = set()

def _connect() -> sqlite3.Connection:
    p = _db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p), check_same_thread=False)
    con.row_factory = sqlite3.Row
    # WAL + NORMAL gives ~3× speedup for bulk inserts on VPS
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA temp_store=MEMORY")
    except Exception:
        pass
    return con

def init_db() -> None:
    p = str(_db_path())
    if p in _DB_INITED:
        return
    con = _connect()
    try:
        con.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            profile_id TEXT NOT NULL,
            target TEXT,
            event_type TEXT NOT NULL,
            success INTEGER NOT NULL,
            duration_ms REAL,
            error TEXT
        )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_profile ON events(profile_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_target ON events(target)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_ts ON events(ts)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_profile_target_ts ON events(profile_id, target, ts)")
        con.commit()
        _DB_INITED.add(p)
    finally:
        con.close()

def record_event(
    profile_id: str,
    target: Optional[str] = None,
    event_type: str = "generic",
    success: bool = True,
    duration_ms: Optional[float] = None,
    error: Optional[str] = None,
    ts: Optional[str] = None,
) -> int:
    init_db()
    if ts is None:
        ts = datetime.now(timezone.utc).isoformat()
    con = _connect()
    try:
        cur = con.execute(
            "INSERT INTO events (ts, profile_id, target, event_type, success, duration_ms, error) VALUES (?,?,?,?,?,?,?)",
            (ts, profile_id, target, event_type, 1 if success else 0, duration_ms, error),
        )
        con.commit()
        return cur.lastrowid
    finally:
        con.close()

def record_events_batch(rows: list[tuple]) -> None:
    """Bulk insert — single transaction, ~20× faster than 1000× record_event."""
    if not rows:
        return
    init_db()
    con = _connect()
    try:
        con.execute("BEGIN")
        con.executemany(
            "INSERT INTO events (ts, profile_id, target, event_type, success, duration_ms, error) VALUES (?,?,?,?,?,?,?)",
            rows,
        )
        con.commit()
    finally:
        con.close()


def _since_iso(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

def get_success_rate(profile_id: Optional[str] = None, target: Optional[str] = None, days: int = 7) -> dict[str, Any]:
    init_db()
    con = _connect()
    try:
        since = _since_iso(days)
        q = "SELECT COUNT(*) as total, SUM(success) as ok FROM events WHERE ts >= ?"
        params: list[Any] = [since]
        if profile_id:
            q += " AND profile_id = ?"
            params.append(profile_id)
        if target:
            q += " AND target = ?"
            params.append(target)
        row = con.execute(q, params).fetchone()
        total = row["total"] or 0
        ok = row["ok"] or 0
        fail = total - ok
        rate = (ok / total) if total else None
        return {"total": total, "success": ok, "fail": fail, "rate": rate, "days": days, "profile_id": profile_id, "target": target}
    finally:
        con.close()

def get_overall_stats(days: int = 7) -> dict[str, Any]:
    return get_success_rate(days=days)

def get_per_target(days: int = 7, limit: int = 20) -> list[dict[str, Any]]:
    init_db()
    con = _connect()
    try:
        since = _since_iso(days)
        rows = con.execute("""
            SELECT target, COUNT(*) as total, SUM(success) as ok
            FROM events WHERE ts >= ? AND target IS NOT NULL AND target != ''
            GROUP BY target ORDER BY total DESC LIMIT ?
        """, (since, limit)).fetchall()
        out = []
        for r in rows:
            total = r["total"]; ok = r["ok"] or 0
            out.append({"target": r["target"], "total": total, "success": ok, "fail": total-ok, "rate": (ok/total) if total else None})
        return out
    finally:
        con.close()

def get_per_profile(days: int = 7) -> list[dict[str, Any]]:
    init_db()
    con = _connect()
    try:
        since = _since_iso(days)
        rows = con.execute("""
            SELECT profile_id, COUNT(*) as total, SUM(success) as ok
            FROM events WHERE ts >= ? GROUP BY profile_id ORDER BY total DESC
        """, (since,)).fetchall()
        out = []
        for r in rows:
            total = r["total"]; ok = r["ok"] or 0
            out.append({"profile_id": r["profile_id"], "total": total, "success": ok, "fail": total-ok, "rate": (ok/total) if total else None})
        return out
    finally:
        con.close()

def get_health_series(profile_id: str, days: int = 30) -> list[dict[str, Any]]:
    """График здоровья: bucket по дням — success/fail для профиля."""
    init_db()
    con = _connect()
    try:
        since = _since_iso(days)
        rows = con.execute("""
            SELECT substr(ts,1,10) as day, COUNT(*) as total, SUM(success) as ok
            FROM events WHERE profile_id = ? AND ts >= ?
            GROUP BY day ORDER BY day
        """, (profile_id, since)).fetchall()
        return [{"day": r["day"], "total": r["total"], "success": r["ok"] or 0, "fail": (r["total"]-(r["ok"] or 0)), "rate": ((r["ok"] or 0)/r["total"]) if r["total"] else None} for r in rows]
    finally:
        con.close()

def get_recent_events(limit: int = 50, profile_id: Optional[str] = None) -> list[dict[str, Any]]:
    init_db()
    con = _connect()
    try:
        if profile_id:
            rows = con.execute("SELECT * FROM events WHERE profile_id=? ORDER BY id DESC LIMIT ?", (profile_id, limit)).fetchall()
        else:
            rows = con.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()

def clear_all() -> None:
    """Только для тестов — очищает таблицу."""
    init_db()
    con = _connect()
    try:
        con.execute("DELETE FROM events")
        con.commit()
    finally:
        con.close()
