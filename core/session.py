"""
Session — EngineAdapter. Единый интерфейс поверх Camoufox (firefox) и Patchright (chromium).
Оптимизирован для агента: headless, без визуала, shared binary, лимит кэша, экономия RAM.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional, Protocol

from .patches import apply_camoufox_patches, fix_camoufox_kwargs

from .profile_manager import Profile


class EngineAdapter(Protocol):
    def launch(self, profile: Profile, headless: bool = True) -> Any: ...
    def close(self) -> None: ...


# --- Camoufox (firefox) ---

def _inject_seeded_cookies(context, profile: Profile) -> int:
    """
    One-shot инъекция кук из profiles/{id}/cookie_seed.json (пишет cookie_farmer.seed_from_bank).
    После инъекции файл удаляется. Возвращает число добавленных кук.
    """
    seed_path = profile.dir / "cookie_seed.json"
    if not seed_path.exists():
        return 0
    try:
        cookies = json.loads(seed_path.read_text(encoding="utf-8"))
        # Playwright требует url или domain+path
        valid = []
        for c in cookies:
            if not c.get("name") or c.get("value") is None:
                continue
            item = {"name": c["name"], "value": str(c["value"])}
            if c.get("url"):
                item["url"] = c["url"]
            elif c.get("domain"):
                item["domain"] = c["domain"]
                item["path"] = c.get("path", "/")
            else:
                continue
            if c.get("expires") and isinstance(c["expires"], (int, float)) and c["expires"] > 0:
                item["expires"] = c["expires"]
            valid.append(item)
        if valid:
            context.add_cookies(valid)
        seed_path.unlink(missing_ok=True)
        return len(valid)
    except Exception:
        return 0


class CamoufoxEngine:
    """
    Агентский режим Camoufox:
    - headless (на linux можно 'virtual' = Xvfb)
    - humanize=False — мы делаем своё поведение в behavior/
    - geoip=True — timezone/locale из прокси
    - enable_cache=False — экономия RAM (кэш страниц выключен)
    - block_images — опционально через AGENTFOX_BLOCK_IMAGES=1
    - firefox_user_prefs — лимит кэша, отключение телеметрии
    """

    def __init__(self) -> None:
        self._cm = None
        self._context = None
        self._page = None

    def launch(self, profile: Profile, headless=True):
        # Level 2 monkey-patch слой (core/patches.py) — идемпотентно, graceful fallback
        try:
            apply_camoufox_patches()
        except Exception:
            pass

        try:
            from camoufox.sync_api import Camoufox
        except ImportError as e:
            raise RuntimeError("cloverlabs-camoufox not installed. pip install cloverlabs-camoufox[geoip]") from e

        proxy = profile.proxy.to_camoufox_proxy() if profile.proxy else None
        block_images = os.getenv("AGENTFOX_BLOCK_IMAGES", "0") == "1"

        # Стабильный отпечаток: детерминированный реальный пресет из bundled JSON.
        # Один профиль → один пресет → одинаковые UA/screen/WebGL между сессиями.
        from .identity import resolve_fingerprint_preset

        preset = resolve_fingerprint_preset(profile.identity.fingerprint_preset_id, profile.identity.os)

        # Стабильные сиды шума: Camoufox рандомит их per-launch ТОЛЬКО если не заданы.
        # Задаём из identity → один профиль = одинаковый canvas/audio/fonts между сессиями.
        import hashlib

        def _seed_int(domain: str) -> int:
            h = hashlib.sha256(f"{profile.identity.id}:{domain}".encode()).hexdigest()
            return int(h[:8], 16)

        # Стабильные сиды шума: Camoufox рандомит их per-launch ТОЛЬКО если не заданы.
        noise_seeds = {
            "canvas:seed": int(profile.identity.canvas_seed, 16) % (2**32),
            "audio:seed": int(profile.identity.audio_seed, 16) % (2**32),
            "fonts:spacing_seed": _seed_int("fonts"),
        }

        # Стабильные subsets fonts/voices: иначе рандомятся каждый запуск
        # и canvas hash плывёт даже при фиксированном сиде.
        import random as _random

        from camoufox.fingerprints import _load_os_fonts, _load_os_voices

        def _stable_subset(loader, domain: str, os_key_map: dict) -> list:
            try:
                data = loader()
                os_key = os_key_map.get(profile.identity.os, "mac")
                full_list = data.get(os_key, [])
                if not full_list:
                    return []
                rng = _random.Random(int(hashlib.sha256(f"{profile.identity.id}:{domain}".encode()).hexdigest(), 16))
                return sorted(rng.sample(full_list, max(1, int(len(full_list) * 0.55))))
            except Exception:
                return []

        stable_fonts = _stable_subset(_load_os_fonts, "fontsubset", {"windows": "win", "macos": "mac", "linux": "lin"})
        stable_voices = _stable_subset(_load_os_voices, "voices", {"windows": "win", "macos": "mac", "linux": "lin"})
        if stable_fonts:
            noise_seeds["fonts"] = stable_fonts
        if stable_voices:
            noise_seeds["voices"] = stable_voices

        camo_kwargs: dict[str, Any] = dict(
            persistent_context=True,
            user_data_dir=str(profile.user_data_dir),
            headless=headless,
            humanize=False,
            geoip=True,
            os=profile.identity.os,
            locale=profile.identity.locale,
            # RAM-оптимизации агента:
            enable_cache=False,           # не кэшировать страницы в памяти
            block_images=block_images,     # экономия трафика/RAM по env
            block_webrtc=True,             # WebRTC палит реальный IP — всегда блок
            # префы браузера (правильный канал, не config)
            firefox_user_prefs={
                "browser.cache.disk.capacity": 51200,       # 50 MB дисковый кэш
                "browser.cache.memory.capacity": 8192,      # 8 MB memory cache
                "browser.safebrowsing.enabled": False,
                "browser.safebrowsing.malware.enabled": False,
                "extensions.pocket.enabled": False,
                "browser.sessionstore.interval": 60000,     # реже писать сессию на диск
            },
        )
        if preset:
            camo_kwargs["fingerprint_preset"] = preset
        camo_kwargs["config"] = noise_seeds
        if proxy:
            camo_kwargs["proxy"] = proxy

        # авто-фикс: если префы случайно попали в config — переносим в firefox_user_prefs
        try:
            camo_kwargs = fix_camoufox_kwargs(camo_kwargs)
        except Exception:
            pass

        self._cm = Camoufox(**camo_kwargs)
        # persistent_context=True → __enter__ возвращает BrowserContext
        self._context = self._cm.__enter__()
        injected = _inject_seeded_cookies(self._context, profile)
        if injected:
            print(f"[agentfox] seeded {injected} cookies into {profile.id}")
        page = self._context.new_page()
        self._page = page
        return page

    @property
    def context(self):
        """Доступ к контексту для add_cookies и т.п."""
        return self._context

    def close(self) -> None:
        if self._cm is not None:
            try:
                self._cm.__exit__(None, None, None)
            except Exception:
                pass
            self._cm = None
            self._context = None
            self._page = None


class PatchrightEngine:
    """Fallback Chromium контур — когда цель режет Firefox TLS."""

    def __init__(self) -> None:
        self._pw = None
        self._cm = None
        self._context = None
        self._ctx = None

    def launch(self, profile: Profile, headless: bool = True):
        try:
            from patchright.sync_api import sync_playwright
        except ImportError as e:
            raise RuntimeError("patchright not installed. pip install patchright && patchright install chromium") from e

        self._pw = sync_playwright().start()
        proxy = profile.proxy.to_playwright_proxy() if profile.proxy else None
        # Patchright — это пропатченный Playwright, API идентичен
        launch_kwargs: dict[str, Any] = dict(headless=headless, args=["--no-sandbox", "--disable-dev-shm-usage"])
        if proxy:
            # patchright/playwright принимает proxy на уровне browser, не context
            launch_kwargs["proxy"] = proxy
        self._browser = self._pw.chromium.launch(**launch_kwargs)
        # persistent-ish: используем user_data через context storageState? Для простоты — launchPersistentContext если нужно
        # Но Patchright лучше через launchPersistentContext для кук
        # Fallback: обычный context + storageState из Camoufox не переносим (разные движки)
        w, h = map(int, profile.identity.screen.split("x"))
        self._ctx = self._browser.new_context(
            viewport={"width": w, "height": h},
            locale=profile.identity.locale,
            timezone_id=profile.identity.timezone,
        )
        page = self._ctx.new_page()
        if os.getenv("AGENTFOX_BLOCK_IMAGES", "0") == "1":
            try:
                page.route("**/*", lambda route: route.abort() if route.request.resource_type == "image" else route.continue_())
            except Exception:
                pass
        return page

    def close(self) -> None:
        try:
            if self._ctx:
                self._ctx.close()
        except Exception:
            pass
        try:
            if self._browser:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._ctx = self._browser = self._pw = None


def get_engine(profile: Profile) -> EngineAdapter:
    if profile.engine == "chromium":
        return PatchrightEngine()
    return CamoufoxEngine()


def auto_fallback_if_needed(profile: Profile) -> bool:
    """Re-export from profile_manager for convenience (H3)."""
    try:
        from .profile_manager import auto_fallback_if_needed as _impl

        return _impl(profile)
    except Exception:
        return False


def maybe_auto_fallback(profile: Profile) -> bool:
    """Alias for auto_fallback_if_needed."""
    return auto_fallback_if_needed(profile)


# --- Shared server mode (один процесс на N контекстов) ---
# Для VPS: держит один browser server и раздаёт wsEndpoint контекстам.
# Экономит 400+ MB RAM на 5 профилях.

class SharedCamoufoxServer:
    """
    Опционально: держит один Camoufox launchServer и подключает контексты по ws.
    Используй когда N>1 и нужна максимальная экономия RAM.
    Требует: python -m camoufox server  (или наш launchServer.js)
    """

    def __init__(self, proxy: Optional[dict] = None):
        self.proxy = proxy
        self._server = None
        self._ws_endpoint: Optional[str] = None

    def start(self) -> str:
        # Лениво — запускаем только если нужен shared режим
        # Здесь заглушка: реальный запуск через camoufox server
        # Для MVP возвращаем None и fallback на обычный launch
        return ""

    def stop(self) -> None:
        pass
