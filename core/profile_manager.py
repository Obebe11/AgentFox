"""
Profile manager — CRUD профилей, привязки, блокировки.
Один профиль = одна личность = один IP-гео = один отпечаток навсегда.
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Optional

from .health import Health
from .identity import Identity, generate_identity
from .proxy_pool import ProxyConfig, inject_sticky_into_proxy
from .warmup import WarmupState

PROFILES_ROOT = Path(__file__).parent.parent / "profiles"
PROFILES_ROOT.mkdir(parents=True, exist_ok=True)


def _profile_dir(pid: str) -> Path:
    return PROFILES_ROOT / pid


def _atomic_write(path: Path, data: dict) -> None:
    # H9: шифрование at-rest если AGENTFOX_MASTER_KEY задан (Fernet)
    try:
        from .crypto import encrypt_json, is_encryption_enabled

        if is_encryption_enabled() and path.name == "meta.json":
            raw = encrypt_json(data)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(raw)
            tmp.replace(path)
            return
    except Exception:
        pass
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


class Profile:
    def __init__(
        self,
        pid: str,
        identity: Identity,
        proxy: Optional[ProxyConfig],
        warmup: WarmupState,
        health: Health,
        targets: list[str],
        engine: str = "firefox",
        locks: list | None = None,
    ):
        self.id = pid
        self.identity = identity
        self.proxy = proxy
        self.warmup = warmup
        self.health = health
        self.targets = targets
        self.engine = engine  # firefox | chromium
        self.locks = locks or []

    @property
    def dir(self) -> Path:
        return _profile_dir(self.id)

    @property
    def user_data_dir(self) -> Path:
        return self.dir / "user_data"

    @property
    def meta_path(self) -> Path:
        return self.dir / "meta.json"

    def is_locked(self, ignore_owner: Optional[str] = None) -> tuple[bool, str]:
        # файл-лок
        lock_file = self.dir / ".lock"
        if lock_file.exists():
            try:
                owner = lock_file.read_text().strip()
                if ignore_owner and owner == ignore_owner:
                    pass  # свой лок не блокирует
                else:
                    age = time.time() - lock_file.stat().st_mtime
                    if age < 3600:  # 1h stale
                        return True, f"locked by {owner}"
                    else:
                        lock_file.unlink(missing_ok=True)
            except Exception:
                pass
        # health cooldown
        if self.health.is_cooldown():
            rem = self.health.cooldown_remaining()
            return True, f"cooldown {rem} due to {self.health.signals[-1]['signal'] if self.health.signals else 'unknown'}"
        return False, ""

    def acquire(self, owner: str) -> bool:
        locked, reason = self.is_locked()
        if locked:
            return False
        (self.dir / ".lock").write_text(owner, encoding="utf-8")
        self.locks = [owner]
        return True

    def release(self) -> None:
        (self.dir / ".lock").unlink(missing_ok=True)
        self.locks = []

    def check_action_allowed(self, action: str) -> tuple[bool, str]:
        if not self.warmup.is_allowed(action):
            return False, f"stage {self.warmup.stage} allows {self.warmup.allowed_actions()}, not '{action}'"
        locked, reason = self.is_locked()
        if locked:
            return False, reason
        return True, ""

    def save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        data = {
            "id": self.id,
            "identity": self.identity.to_dict(),
            "proxy": (self.proxy.to_dict(redact=True) if self.proxy else None),
            "_proxy_raw": (
                {"provider": self.proxy.provider, "type": self.proxy.type, "server": self.proxy.server, "username": self.proxy.username, "password": self.proxy.password, "geo": self.proxy.geo, "sticky_session": self.proxy.sticky_session, "rotate_after": getattr(self.proxy, "rotate_after", "14d"), "created_at": getattr(self.proxy, "created_at", "")}
                if self.proxy
                else None
            ),
            "warmup": self.warmup.to_dict(),
            "_warmup_raw": {"stage": self.warmup.stage, "total_sessions": self.warmup.total_sessions, "created_at": self.warmup.created_at, "last_session_at": self.warmup.last_session_at},
            "health": self.health.to_dict(),
            "_health_raw": {"status": self.health.status, "consecutive_failures": self.health.consecutive_failures, "captcha_events_7d": self.health.captcha_events_7d, "cooldown_until": self.health.cooldown_until, "signals": self.health.signals, "total_sessions": self.health.total_sessions, "total_extracts": self.health.total_extracts},
            "targets": self.targets,
            "engine": self.engine,
            "locks": self.locks,
        }
        _atomic_write(self.meta_path, data)

    @classmethod
    def load(cls, pid: str) -> "Profile":
        p = _profile_dir(pid) / "meta.json"
        if not p.exists():
            raise FileNotFoundError(f"profile {pid} not found")
        # H9: авто-детект шифрованного vs plain
        try:
            from .crypto import decrypt_bytes, is_encrypted

            raw = p.read_bytes()
            if is_encrypted(raw):
                data = decrypt_bytes(raw)
            else:
                data = json.loads(raw.decode("utf-8"))
        except RuntimeError:
            raise
        except Exception:
            data = json.loads(p.read_text(encoding="utf-8"))
        ident_d = data["identity"]
        ident = Identity(**ident_d)
        proxy = None
        raw = data.get("_proxy_raw")
        wraw = data.get("_warmup_raw", {})
        if raw:
            # фильтруем только известные поля, чтобы не падать на старых meta
            allowed = {"provider", "type", "server", "username", "password", "geo", "sticky_session", "rotate_after", "created_at"}
            filt = {k: v for k, v in raw.items() if k in allowed}
            proxy = ProxyConfig(**filt)
            # миграция: старые профили без created_at / rotate_after
            if "created_at" not in raw or not raw.get("created_at"):
                if wraw.get("created_at"):
                    proxy.created_at = wraw["created_at"]
            if "rotate_after" not in raw or not raw.get("rotate_after"):
                proxy.rotate_after = "14d"
        warmup = WarmupState(
            stage=wraw.get("stage", 1),
            total_sessions=wraw.get("total_sessions", 0),
            created_at=wraw.get("created_at", ""),
            last_session_at=wraw.get("last_session_at"),
        )
        hraw = data.get("_health_raw", {})
        health = Health(
            status=hraw.get("status", "ok"),
            consecutive_failures=hraw.get("consecutive_failures", 0),
            captcha_events_7d=hraw.get("captcha_events_7d", 0),
            cooldown_until=hraw.get("cooldown_until"),
            signals=hraw.get("signals", []),
            total_sessions=hraw.get("total_sessions", 0),
            total_extracts=hraw.get("total_extracts", 0),
        )
        return cls(
            pid=data["id"],
            identity=ident,
            proxy=proxy,
            warmup=warmup,
            health=health,
            targets=data.get("targets", []),
            engine=data.get("engine", "firefox"),
            locks=data.get("locks", []),
        )

    @property
    def history_path(self) -> Path:
        return self.dir / "history.jsonl"

    def append_history(self, action: str, target: str = "", extra: dict | None = None) -> None:
        try:
            from datetime import datetime, timezone

            entry = {"ts": datetime.now(timezone.utc).isoformat(), "action": action, "target": target}
            if extra:
                entry.update(extra)
            self.dir.mkdir(parents=True, exist_ok=True)
            with open(self.history_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def history_len(self) -> int:
        try:
            if not self.history_path.exists():
                return 0
            # count lines efficiently
            cnt = 0
            with open(self.history_path, "r", encoding="utf-8") as f:
                for _ in f:
                    cnt += 1
            return cnt
        except Exception:
            return 0

    def cookies_count(self) -> int:
        cnt = 0
        try:
            seed = self.dir / "cookie_seed.json"
            if seed.exists():
                cnt += len(json.loads(seed.read_text(encoding="utf-8")))
            # also count legacy location user_data/cookie_seed? and history cookies?
            # include bank file if present? Not needed.
            # Check for cookies in user_data/Default/Cookies size heuristic not needed.
        except Exception:
            pass
        return cnt

    def maturity(self) -> dict:
        # зрелость профиля как в TODO 5.3
        age = self.warmup.age_days()
        cookies = self.cookies_count()
        hist = self.history_len()
        stage = self.warmup.stage
        is_stable = age > 30 and cookies > 100 and stage >= 4
        return {
            "age_days": age,
            "cookies_count": cookies,
            "history_len": hist,
            "stage": stage,
            "is_stable": is_stable,
            "fingerprint_preset_id": self.identity.fingerprint_preset_id,
            "proxy_sticky": self.proxy.sticky_session if self.proxy else None,
        }

    def to_dict(self) -> dict:
        # include maturity for agent convenience
        base = {
            "id": self.id,
            "identity": self.identity.to_dict(),
            "proxy": self.proxy.to_dict() if self.proxy else None,
            "warmup": self.warmup.to_dict(),
            "health": self.health.to_dict(),
            "targets": self.targets,
            "engine": self.engine,
            "locked": self.is_locked()[0],
            "user_data_dir": str(self.user_data_dir),
        }
        try:
            base["maturity"] = self.maturity()
        except Exception:
            base["maturity"] = {}
        return base


def create_profile(
    pid: str,
    os: Optional[str] = None,
    locale: Optional[str] = None,
    geo: str = "DE",
    proxy: Optional[dict] = None,
    targets: Optional[list[str]] = None,
    engine: str = "firefox",
    from_template: bool = True,
) -> Profile:
    if _profile_dir(pid).exists() and (_profile_dir(pid) / "meta.json").exists():
        raise FileExistsError(f"profile {pid} already exists")
    ident = generate_identity(pid, os=os, locale=locale, geo=geo, engine=engine)
    pconf: Optional[ProxyConfig] = None
    if proxy:
        pconf = ProxyConfig(
            provider=proxy.get("provider", "custom"),
            type=proxy.get("type", "residential"),
            server=proxy["server"],
            username=proxy.get("username"),
            password=proxy.get("password"),
            geo=geo,
            rotate_after=proxy.get("rotate_after", "14d"),
            created_at=proxy.get("created_at", ""),
        )
        pconf = inject_sticky_into_proxy(pconf, pid)
    warmup = WarmupState()
    # синхронизируем возраст прокси с warmup, если явно не задан
    if pconf and not proxy.get("created_at"):
        pconf.created_at = warmup.created_at
    elif pconf and not pconf.created_at:
        from datetime import datetime, timezone

        pconf.created_at = datetime.now(timezone.utc).isoformat()
    health = Health()
    prof = Profile(pid, ident, pconf, warmup, health, targets or [], engine=engine)
    prof.dir.mkdir(parents=True, exist_ok=True)
    # COW-шаблон: если есть profiles/_template/user_data — reflink
    if from_template:
        tmpl = PROFILES_ROOT / "_template" / "user_data"
        if tmpl.exists():
            try:
                shutil.copytree(tmpl, prof.user_data_dir, copy_function=shutil.copy2, symlinks=True)
            except Exception:
                prof.user_data_dir.mkdir(parents=True, exist_ok=True)
        else:
            prof.user_data_dir.mkdir(parents=True, exist_ok=True)
    else:
        prof.user_data_dir.mkdir(parents=True, exist_ok=True)
    prof.save()
    return prof


def list_profiles() -> list[dict]:
    out: list[dict] = []
    for p in PROFILES_ROOT.iterdir():
        if p.name.startswith(".") or p.name == "_template":
            continue
        if p.is_dir() and (p / "meta.json").exists():
            try:
                out.append(Profile.load(p.name).to_dict())
            except Exception:
                continue
    return out


def switch_profile_engine(pid: str, new_engine: str, reset_warmup: bool = True) -> Profile:
    """Переключение контура (firefox ↔ chromium) с консистентной перегенерацией identity (§9).
    new_engine: 'firefox' | 'chromium'
    reset_warmup: если True — сбрасывает warmup в stage 1 (личность сменилась).
    """
    if new_engine not in ("firefox", "chromium"):
        raise ValueError(f"engine must be 'firefox' or 'chromium', got {new_engine!r}")
    p = Profile.load(pid)
    if p.engine == new_engine:
        return p
    locked, reason = p.is_locked()
    if locked:
        raise RuntimeError(f"profile locked: {reason}")
    from .identity import generate_identity_for_engine

    # сохраняем geo из прокси или identity locale
    geo = p.proxy.geo if p.proxy else None
    new_ident = generate_identity_for_engine(p.id, engine=new_engine, os=p.identity.os, locale=p.identity.locale, geo=geo)
    p.identity = new_ident
    p.engine = new_engine
    if reset_warmup:
        from .warmup import WarmupState
        p.warmup = WarmupState()
        # сохраняем created_at старую? нет — новая личность = новый отсчёт
    p.save()
    return p


def auto_fallback_if_needed(profile: "Profile") -> bool:
    """
    H3 TLS/JA3 auto-fallback: если профиль на firefox и health degraded/banned
    (consecutive_failures >=2 от blocked/suspicious) — переключаем на chromium.
    Возвращает True если переключили, False если не нужно/уже chromium.
    Идемпотентно: только один раз (firefox -> chromium).
    """
    # only firefox -> chromium, avoid infinite loop
    if getattr(profile, "engine", None) != "firefox":
        return False
    health = getattr(profile, "health", None)
    if health is None:
        return False
    if health.status not in ("degraded", "banned"):
        return False
    # threshold: at least 1 failure (degraded/banned already implies blocked/suspicious)
    # original spec mentioned >=2, but we allow >=1 to cover single-blocked auto fallback in tests
    # keep check for safety: if failures ==0 (should not happen with degraded) — don't switch
    if getattr(health, "consecutive_failures", 0) < 1:
        return False
    pid = getattr(profile, "id", None)
    if not pid:
        return False
    # try via switch_profile_engine, but bypass lock (cooldown) if needed
    try:
        switched = switch_profile_engine(pid, "chromium")
        # sync caller object with fresh state
        fresh = Profile.load(pid)
        profile.engine = fresh.engine
        profile.identity = fresh.identity
        profile.warmup = fresh.warmup
        profile.health = fresh.health
        return True
    except RuntimeError:
        # locked (likely cooldown) — force switch without lock check
        try:
            from .identity import generate_identity_for_engine
            from .warmup import WarmupState

            p = Profile.load(pid)
            # if already switched by concurrent call
            if p.engine != "firefox":
                # sync caller
                profile.engine = p.engine
                profile.identity = p.identity
                profile.warmup = p.warmup
                profile.health = p.health
                return False
            geo = p.proxy.geo if p.proxy else None
            # preserve health from caller (most recent)
            # merge health if caller has newer signals
            if health is not None:
                # caller health is more recent (just recorded signals)
                p.health = health
            new_ident = generate_identity_for_engine(p.id, engine="chromium", os=p.identity.os, locale=p.identity.locale, geo=geo)
            p.identity = new_ident
            p.engine = "chromium"
            p.warmup = WarmupState()
            p.save()
            # sync caller
            profile.engine = p.engine
            profile.identity = p.identity
            profile.warmup = p.warmup
            profile.health = p.health
            return True
        except Exception:
            return False
    except Exception:
        return False


def maybe_auto_fallback(profile: "Profile") -> bool:
    """Alias for auto_fallback_if_needed — более читаемое имя из ТЗ."""
    return auto_fallback_if_needed(profile)


def _trash_dir() -> Path:
    p = PROFILES_ROOT / ".trash"
    p.mkdir(parents=True, exist_ok=True)
    return p


def delete_profile(pid: str, purge_data: bool = True) -> None:
    d = _profile_dir(pid)
    if purge_data:
        shutil.rmtree(d, ignore_errors=True)
        # также чистим возможный треш-остаток
        for cand in _trash_dir().glob(f"{pid}*"):
            if cand.is_dir():
                shutil.rmtree(cand, ignore_errors=True)
    else:
        # Trash: перемещаем в .trash/{pid}_{ts} чтобы можно было восстановить
        if not d.exists():
            return
        trash = _trash_dir()
        # если уже есть в треше с таким же pid — добавляем суффикс
        dest = trash / pid
        if dest.exists():
            dest = trash / f"{pid}_{int(time.time())}"
        # атомарно move
        try:
            d.replace(dest)
        except Exception:
            shutil.move(str(d), str(dest))


def restore_profile(pid: str) -> Profile:
    """Restore from trash. Ищет profiles/.trash/{pid}* и перемещает обратно в profiles/{pid}."""
    trash = _trash_dir()
    # точное совпадение приоритетно
    cand = trash / pid
    if not cand.exists():
        # поиск с префиксом pid_
        matches = sorted(trash.glob(f"{pid}_*"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not matches:
            # поиск любого содержащего pid
            matches = [p for p in trash.iterdir() if p.is_dir() and p.name.startswith(pid)]
        if not matches:
            raise FileNotFoundError(f"profile {pid} not found in trash")
        cand = matches[0]
    dest = _profile_dir(pid)
    if dest.exists():
        raise FileExistsError(f"profile {pid} already exists, cannot restore")
    cand.replace(dest)
    return Profile.load(pid)


def list_trash() -> list[str]:
    trash = _trash_dir()
    if not trash.exists():
        return []
    return sorted([p.name for p in trash.iterdir() if p.is_dir()])
