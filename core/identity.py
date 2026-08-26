"""
Identity — стабильная личность профиля.
Главный инвариант: fingerprint пинится навсегда, сиды шума фиксированы.
"""
from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, asdict
from typing import Literal, Optional

OS_CHOICES: list[Literal["windows", "macos", "linux"]] = ["windows", "macos", "linux"]

# Веса как в реальном трафике — Linux редкий, поэтому его мало среди личностей
OS_WEIGHTS = {"windows": 0.72, "macos": 0.20, "linux": 0.08}

LOCALE_BY_OS = {
    "windows": ["en-US", "de-DE", "fr-FR", "ru-RU", "es-ES", "en-CA"],
    "macos": ["en-US", "en-GB", "fr-FR", "de-DE", "en-CA"],
    "linux": ["en-US", "de-DE", "ru-RU", "en-CA"],
}

TZ_BY_LOCALE = {
    "en-US": "America/New_York",
    "en-GB": "Europe/London",
    "en-CA": "America/Toronto",
    "de-DE": "Europe/Berlin",
    "fr-FR": "Europe/Paris",
    "ru-RU": "Europe/Moscow",
    "es-ES": "Europe/Madrid",
    "fr-CA": "America/Toronto",
}

SCREEN_BY_OS = {
    "windows": ["1920x1080", "1366x768", "2560x1440", "1920x1200"],
    "macos": ["2560x1600", "1440x900", "1920x1080", "3024x1964"],
    "linux": ["1920x1080", "1366x768", "2560x1440"],
}


@dataclass(frozen=True)
class Identity:
    id: str
    os: str
    locale: str
    timezone: str
    screen: str
    fingerprint_preset_id: str  # стабильный сид для Canvas/WebGL шума
    canvas_seed: str
    webgl_seed: str
    audio_seed: str

    def to_camoufox_config(self) -> dict:
        w, h = map(int, self.screen.split("x"))
        return {
            "os": self.os,
            # fingerprint_preset фиксирует сиды ниже, но явно передаём для изоляции
            "fingerprint_preset": self.fingerprint_preset_id,
            "canvas_seed": self.canvas_seed,
            "webgl_seed": self.webgl_seed,
            "audio_seed": self.audio_seed,
            "locale": self.locale,
            "timezone": self.timezone,
            "screen": {"width": w, "height": h},
        }

    def to_dict(self) -> dict:
        return asdict(self)


def _seeded_choice(rng: random.Random, items: list, weights: list | None = None) -> str:
    if weights:
        return rng.choices(items, weights=weights, k=1)[0]
    return rng.choice(items)


def generate_identity(
    profile_id: str,
    os: Optional[str] = None,
    locale: Optional[str] = None,
    geo: Optional[str] = None,
    engine: str = "firefox",
) -> Identity:
    """
    Детерминированная генерация от profile_id — одна личность навсегда.
    Если os/locale/geo переданы — уважает их, но сиды всё равно детерминированы.
    engine влияет на детерминизм (firefox vs chromium дают разные, но стабильные отпечатки).
    """
    # engine-aware hash: firefox сохраняет совместимость со старыми профилями (hash без суффикса),
    # chromium — отдельный детерминированный набор (чтобы переключение контура давало консистентный, но отличный отпечаток)
    hash_input = profile_id if engine == "firefox" else f"{profile_id}:{engine}"
    h = hashlib.sha256(hash_input.encode()).hexdigest()
    rng = random.Random(int(h[:16], 16))

    chosen_os = os or _seeded_choice(
        rng, OS_CHOICES, [OS_WEIGHTS[o] for o in OS_CHOICES]
    )
    # geo → locale hint
    if geo and not locale:
        geo_locale = {"DE": "de-DE", "FR": "fr-FR", "RU": "ru-RU", "US": "en-US", "GB": "en-GB", "ES": "es-ES", "CA": "en-CA"}.get(
            geo.upper()
        )
        if geo_locale:
            locale = geo_locale

    chosen_locale = locale or rng.choice(LOCALE_BY_OS.get(chosen_os, ["en-US"]))
    tz = TZ_BY_LOCALE.get(chosen_locale, "Europe/Berlin")
    screen = rng.choice(SCREEN_BY_OS.get(chosen_os, ["1920x1080"]))

    # Стабильные сиды — хеш от profile_id + домена (+engine для изоляции контуров)
    def _sid(domain: str) -> str:
        base = profile_id if engine == "firefox" else f"{profile_id}:{engine}"
        return hashlib.sha256(f"{base}:{domain}".encode()).hexdigest()[:16]

    return Identity(
        id=profile_id,
        os=chosen_os,
        locale=chosen_locale,
        timezone=tz,
        screen=screen,
        fingerprint_preset_id=_sid("preset"),
        canvas_seed=_sid("canvas"),
        webgl_seed=_sid("webgl"),
        audio_seed=_sid("audio"),
    )


def load_browserforge_preset(preset_id: str, version: str = "v150") -> dict | None:
    """Загружает реальный preset если доступен (опционально)."""
    try:
        import importlib.resources as res

        text = res.files("camoufox").joinpath(f"fingerprint-presets-{version}.json").read_text()
        data = json.loads(text)
        # presets — список dict с UA и т.д., берём детерминированный индекс
        idx = int(hashlib.sha256(preset_id.encode()).hexdigest(), 16) % len(data)
        return data[idx]
    except Exception:
        return None


_PRESETS_CACHE: dict[str, Optional[dict]] = {}


def _preset_webgl_ok(preset: dict, os_name: str) -> bool:
    """Проверяет что WebGL-комбо пресета есть в данных пакета (часть альфа-пресетов битая)."""
    webgl = preset.get("webgl") or {}
    vendor = webgl.get("unmaskedVendor")
    renderer = webgl.get("unmaskedRenderer")
    if not vendor or not renderer:
        return True  # нет webgl в пресете — ок
    try:
        from camoufox.webgl.sample import sample_webgl

        os_key = {"windows": "win", "macos": "mac", "linux": "lin"}.get(os_name, "win")
        sample_webgl(os_key, vendor, renderer)
        return True
    except Exception:
        return False


def generate_identity_for_engine(
    profile_id: str,
    engine: str,
    os: Optional[str] = None,
    locale: Optional[str] = None,
    geo: Optional[str] = None,
) -> Identity:
    """Консистентный ребрендинг под движок (ARCHITECTURE.md §9, Фаза 3.2).
    Firefox и Chromium дают разные, но детерминированные личности для одного profile_id.
    """
    if engine not in ("firefox", "chromium"):
        raise ValueError(f"unknown engine {engine!r}")
    return generate_identity(profile_id, os=os, locale=locale, geo=geo, engine=engine)


def resolve_fingerprint_preset(preset_id: str, os_name: str) -> Optional[dict]:
    """
    Детерминированный выбор реального пресета из bundled fingerprints.
    Один profile_id + os → всегда один и тот же пресет (UA, screen, WebGL).
    Битые WebGL-комбо (баг альфы) пропускаются детерминированным обходом.
    """
    cache_key = f"{preset_id}:{os_name}"
    if cache_key in _PRESETS_CACHE:
        return _PRESETS_CACHE[cache_key]
    result: Optional[dict] = None
    try:
        import importlib.resources as res

        text = res.files("camoufox").joinpath("fingerprint-presets-v150.json").read_text()
        data = json.loads(text)
        by_os = data.get("presets", {})
        items = by_os.get(os_name) or by_os.get("windows") or []
        if items:
            start = int(hashlib.sha256(preset_id.encode()).hexdigest(), 16) % len(items)
            for offset in range(len(items)):
                candidate = items[(start + offset) % len(items)]
                if _preset_webgl_ok(candidate, os_name):
                    result = candidate
                    break
    except Exception:
        result = None
    _PRESETS_CACHE[cache_key] = result
    return result
