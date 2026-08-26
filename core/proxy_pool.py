"""
Proxy pool — sticky sessions, health-check, geo matching.
Один профиль = один sticky IP навсегда (инвариант связываемости).

Поддерживаемые форматы sticky у провайдеров:
- username-suffix: IPRoyal `-session-X`, Bright Data `-session-X`, ProxyEmpire `-session-X`
- host-prefix: некоторые дают отдельный шлюз на стики
- ip-whitelist: sticky на стороне провайдера, нам делать нечего
"""
from __future__ import annotations

import datetime
import hashlib
import re
import socket
import time
from dataclasses import dataclass, asdict, field
from datetime import timezone, timedelta, datetime as dt
from typing import Optional

STICKY_STYLE_SUFFIX_PROVIDERS = {
    "iproyal": "-session-{sid}",
    "brightdata": "-session-{sid}",
    "proxyempire": "-session-{sid}",
    "smartproxy": "-session-{sid}",
    # oxylabs: sesssid добавляется к базовому username как суффикс
    "oxylabs": "-sesssid-{sid}",
    "generic": "-session-{sid}",
}


@dataclass
class ProxyConfig:
    provider: str = "custom"  # iproyal | brightdata | smartproxy | oxylabs | custom...
    type: str = "residential"  # residential | mobile | datacenter
    server: str = ""  # http://host:port
    username: Optional[str] = None
    password: Optional[str] = None
    geo: str = "DE"
    sticky_session: str = ""  # sticky ID — одинаковый у одного профиля всегда
    rotate_after: str = "14d"  # e.g. "14d", "7d", "24h" — авто-ротация после истечения
    created_at: str = ""  # ISO8601 — когда sticky был выдан/последний rotate
    _health: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.created_at:
            # устанавливается при создании; при загрузке из _proxy_raw уже будет значение
            try:
                object.__setattr__(self, "created_at", dt.now(timezone.utc).isoformat())
            except Exception:
                pass

    def to_playwright_proxy(self) -> dict:
        d: dict = {"server": self.server}
        if self.username:
            d["username"] = self.username
        if self.password:
            d["password"] = self.password
        return d

    def to_camoufox_proxy(self) -> dict:
        return self.to_playwright_proxy()

    def apply_sticky(self, profile_id: str) -> None:
        """
        Встраивает sticky session в username по формату провайдера.
        Вызывается один раз при создании профиля; после этого username фиксирован.
        """
        if not self.sticky_session:
            self.sticky_session = make_sticky_session(profile_id)
        style = STICKY_STYLE_SUFFIX_PROVIDERS.get(
            self.provider.lower(), STICKY_STYLE_SUFFIX_PROVIDERS["generic"]
        )
        suffix = style.format(sid=self.sticky_session)
        base_user = self._base_username()
        if provider_uses_username_suffix(self.provider):
            self.username = f"{base_user}{suffix}"

    def _base_username(self) -> str:
        """username без ранее добавленного суффикса (идемпотентность)."""
        u = self.username or ""
        for style in STICKY_STYLE_SUFFIX_PROVIDERS.values():
            # грубая идемпотентность: отрезаем известные маркеры
            for marker in ("-session-", "-sesssid-"):
                idx = u.find(marker)
                if idx != -1:
                    return u[:idx]
        return u

    def to_dict(self, redact: bool = True) -> dict:
        d = asdict(self)
        d.pop("_health", None)
        if redact and d.get("password"):
            d["password"] = "***"
        return d


def provider_uses_username_suffix(provider: str) -> bool:
    return provider.lower() in STICKY_STYLE_SUFFIX_PROVIDERS


def make_sticky_session(profile_id: str) -> str:
    return hashlib.sha256(profile_id.encode()).hexdigest()[:12]


def inject_sticky_into_proxy(proxy: ProxyConfig, profile_id: str) -> ProxyConfig:
    """Обёртка совместимости: применяет sticky по формату провайдера."""
    proxy.apply_sticky(profile_id)
    return proxy


# --- health-check ---

def check_proxy(proxy: ProxyConfig, timeout: float = 10.0) -> dict:
    """
    Быстрый health-check через сам прокси: внешний IP + страна.
    Возвращает {'ok': bool, 'ip': str, 'country': str|None, 'latency_ms': int}
    Страну достаём из ответа провайдера если есть; иначе ip-api.
    """
    import httpx

    result: dict = {"ok": False, "ip": None, "country": None, "latency_ms": -1}
    proxy_url = proxy.server
    if proxy.username and proxy.password:
        # вставить creds в URL для httpx
        s = proxy.server
        if "://" in s:
            scheme, rest = s.split("://", 1)
            proxy_url = f"{scheme}://{proxy.username}:{proxy.password}@{rest}"
        else:
            proxy_url = f"http://{proxy.username}:{proxy.password}@{s}"

    t0 = time.time()
    try:
        with httpx.Client(proxy=proxy_url, timeout=timeout) as client:
            r = client.get("http://ip-api.com/json/?fields=query,countryCode,status")
            result["latency_ms"] = int((time.time() - t0) * 1000)
            data = r.json()
            if data.get("status") == "success":
                result["ok"] = True
                result["ip"] = data.get("query")
                result["country"] = data.get("countryCode")
    except Exception as e:
        result["error"] = str(e)[:200]
    return result


def resolve_host(host: str, timeout: float = 5.0) -> bool:
    try:
        socket.setdefaulttimeout(timeout)
        hostname = host.split("://")[-1].split(":")[0]
        socket.gethostbyname(hostname)
        return True
    except Exception:
        return False


# --- rotate helpers ---

def _parse_rotate_after(value) -> timedelta:
    """Парсит '14d', '7d', '24h', int дни → timedelta. Дефолт 14d."""
    if isinstance(value, timedelta):
        return value
    if isinstance(value, (int, float)):
        return timedelta(days=float(value))
    if isinstance(value, str):
        v = value.strip().lower()
        m = re.match(r"^\s*(\d+)\s*([dhm])\s*$", v)
        if m:
            num = int(m.group(1))
            unit = m.group(2)
            if unit == "d":
                return timedelta(days=num)
            if unit == "h":
                return timedelta(hours=num)
            if unit == "m":
                return timedelta(minutes=num)
        # fallback: plain int string
        try:
            return timedelta(days=int(v))
        except Exception:
            pass
    return timedelta(days=14)


def should_rotate(proxy: ProxyConfig, now=None, created_at: Optional[str] = None) -> bool:
    """
    Проверяет истёк ли rotate_after с момента created_at.
    - proxy.rotate_after: "14d" по умолчанию
    - created_at: ISO8601 строка; если не передана — берёт proxy.created_at
    - now: datetime или ISO строка; по умолчанию utcnow
    Возвращает True если пора ротировать.
    """
    # resolve created reference
    ref = created_at if created_at is not None else getattr(proxy, "created_at", None)
    if not ref:
        return False
    try:
        # parse now
        if now is None:
            now_dt = dt.now(timezone.utc)
        elif isinstance(now, str):
            now_dt = dt.fromisoformat(now)
        elif isinstance(now, dt):
            now_dt = now
        else:
            now_dt = dt.now(timezone.utc)
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=timezone.utc)
        # parse ref
        if isinstance(ref, str):
            created_dt = dt.fromisoformat(ref)
        elif isinstance(ref, dt):
            created_dt = ref
        else:
            return False
        if created_dt.tzinfo is None:
            created_dt = created_dt.replace(tzinfo=timezone.utc)
        rotate_after = getattr(proxy, "rotate_after", "14d") or "14d"
        delta = _parse_rotate_after(rotate_after)
        return (now_dt - created_dt) > delta
    except Exception:
        return False


def rotate_proxy_if_needed(profile) -> bool:
    """
    Проверяет профиль на истечение sticky (rotate_after 14d по умолчанию).
    Источник возраста: profile.proxy.created_at -> fallback profile.warmup.created_at
    Если любой из них старше rotate_after — ротируем. Это покрывает миграцию старых профилей
    где proxy.created_at мог быть переустановлен в now при загрузке.
    Если истёк — генерирует новый sticky_session и пере-injects в username, обновляет created_at.
    Возвращает True если ротация произошла.
    Сохранение profile на вызывающем (api/server).
    """
    proxy = getattr(profile, "proxy", None)
    if not proxy:
        return False
    warmup = getattr(profile, "warmup", None)
    proxy_created = getattr(proxy, "created_at", None)
    warmup_created = getattr(warmup, "created_at", None) if warmup else None
    # нужна ли ротация: если любой источник старше rotate_after
    need_rotate = False
    if proxy_created and should_rotate(proxy, created_at=proxy_created):
        need_rotate = True
    if not need_rotate and warmup_created and should_rotate(proxy, created_at=warmup_created):
        need_rotate = True
    if not need_rotate and not proxy_created and warmup_created:
        need_rotate = should_rotate(proxy, created_at=warmup_created)
    if not need_rotate:
        return False
    # генерируем новый sticky — случайный, чтобы IP сменился
    import secrets

    new_sid = hashlib.sha256(f"{profile.id}:{time.time()}:{secrets.token_hex(8)}".encode()).hexdigest()[:12]
    proxy.sticky_session = new_sid
    try:
        proxy.apply_sticky(profile.id)
        proxy.sticky_session = new_sid
        base_user = proxy._base_username()
        style = STICKY_STYLE_SUFFIX_PROVIDERS.get(
            proxy.provider.lower(), STICKY_STYLE_SUFFIX_PROVIDERS["generic"]
        )
        suffix = style.format(sid=new_sid)
        if provider_uses_username_suffix(proxy.provider):
            proxy.username = f"{base_user}{suffix}"
    except Exception:
        try:
            inject_sticky_into_proxy(proxy, profile.id)
            proxy.sticky_session = new_sid
        except Exception:
            pass
    try:
        proxy.created_at = dt.now(timezone.utc).isoformat()
    except Exception:
        pass
    return True


# alias для совместимости с TODO/исследованием
def rotate_if_expired(proxy: ProxyConfig, profile_id: str, created_at: Optional[str] = None, now=None) -> bool:
    """Совместимость: проверяет proxy на истечение и ротирует если нужно. Возвращает bool ротации."""
    # создаём временный профиль-подобный объект если нет профиля
    if not should_rotate(proxy, now=now, created_at=created_at):
        return False
    import secrets

    new_sid = hashlib.sha256(f"{profile_id}:{time.time()}:{secrets.token_hex(8)}".encode()).hexdigest()[:12]
    proxy.sticky_session = new_sid
    inject_sticky_into_proxy(proxy, profile_id)
    proxy.sticky_session = new_sid
    # fix username suffix again
    try:
        base_user = proxy._base_username()
        style = STICKY_STYLE_SUFFIX_PROVIDERS.get(
            proxy.provider.lower(), STICKY_STYLE_SUFFIX_PROVIDERS["generic"]
        )
        suffix = style.format(sid=new_sid)
        if provider_uses_username_suffix(proxy.provider):
            proxy.username = f"{base_user}{suffix}"
    except Exception:
        pass
    try:
        proxy.created_at = dt.now(timezone.utc).isoformat()
    except Exception:
        pass
    return True


# --- health gate helper (bool wrapper, mock-friendly) ---

def check_proxy_health(proxy: ProxyConfig, timeout: float = 5.0) -> bool:
    """
    Bool-обёртка над check_proxy для гейта в api/server.
    Возвращает True если прокси здоров (ok), False если нет.
    Бросает исключение только при ошибке вызова — caller решает best-effort.
    Тестируется через мок (не требует сети).
    """
    res = check_proxy(proxy, timeout=timeout)
    return bool(res.get("ok"))
