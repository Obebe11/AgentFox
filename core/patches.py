"""
core.patches — monkey-patch слой Level 2 (ARCHITECTURE.md §9).

До перехода на форк репозитория (daijro/camoufox) носим правки здесь.
Когда патчей >3 или нужен глубокий фикс pythonlib — переносим в форк и делаем rebase.

Патчи документированы, идемпотентны, импортируются без установленного camoufox
(graceful fallback — check_environment вернёт skipped/warn вместо падения).

Покрывает известные ограничения бинаря 152.0.4-beta.28:
- playwright <1.61 guard (daijro#653)
- firefox_user_prefs vs config канал
- canvas/audio seed stability (beta.28 per-launch шум)
- WebGL preset validation для битых альфа-пресетов
"""
from __future__ import annotations

import hashlib
import logging
import warnings
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Константы / ссылки на апстрим-проблемы
PLAYWRIGHT_MAX_VERSION = (1, 61)  # <1.61, 1.61 ломает Juggler viewport.isMobile
PLAYWRIGHT_MAX_VERSION_STR = "1.61"
ISSUE_PLAYWRIGHT_653 = "https://github.com/daijro/camoufox/issues/653"
ISSUE_CANVAS_SEED = "https://github.com/daijro/camoufox/issues/653"  # та же ветка, fingerprint-per-context
BETA_VERSION = "152.0.4-beta.28"

# Идемпотентность — защита от двойного патча при повторных импортах / вызовах
_PATCHED = False
_PATCH_REPORT: dict[str, Any] | None = None

# Ключи которые ОБЯЗАНЫ идти через firefox_user_prefs, а не config.
# config принимает только fingerprint-свойства (canvas/audio/fonts и т.д.);
# любые browser.* — только через firefox_user_prefs (см. session.py camo_kwargs).
# Если ключ содержит точку — почти наверняка это pref.
_PREF_KEY_HINT = "."  # эвристика: любой ключ с точкой → pref, не fingerprint-свойство


# ---------------------------------------------------------------------------
# 1. Playwright version guard
# ---------------------------------------------------------------------------

def _parse_version_tuple(v: str) -> tuple[int, ...]:
    """Парсит '1.60.0' → (1, 60, 0). Игнорирует суффиксы типа '1.60.0a1'."""
    parts: list[int] = []
    for chunk in v.strip().split("."):
        num = ""
        for ch in chunk:
            if ch.isdigit():
                num += ch
            else:
                break
        if num:
            parts.append(int(num))
        else:
            break
    return tuple(parts) if parts else (0,)


def get_playwright_version() -> Optional[str]:
    """Возвращает версию установленного playwright или None если не установлен."""
    try:
        from importlib.metadata import version as _pkg_version

        return _pkg_version("playwright")
    except Exception:
        return None


def is_playwright_version_compatible(version_str: Optional[str] = None) -> bool:
    """
    True если версия <1.61 (совместима с Camoufox Juggler).
    Если version_str is None — берёт установленную версию; если playwright не установлен — True
    (нечего проверять, fallback).
    """
    v = version_str if version_str is not None else get_playwright_version()
    if v is None:
        return True
    parsed = _parse_version_tuple(v)
    # сравниваем только major.minor
    major_minor = parsed[:2] if len(parsed) >= 2 else parsed + (0,) * (2 - len(parsed))
    return major_minor < PLAYWRIGHT_MAX_VERSION


def check_playwright_version(version_str: Optional[str] = None) -> dict[str, Any]:
    """
    Проверяет playwright версию.
    Возвращает dict с ключами ok, installed_version, required, issue, message.
    """
    installed = version_str if version_str is not None else get_playwright_version()
    if installed is None:
        return {
            "ok": True,
            "status": "skipped",
            "installed_version": None,
            "required": f"<{PLAYWRIGHT_MAX_VERSION_STR}",
            "issue": ISSUE_PLAYWRIGHT_653,
            "message": "playwright не установлен — проверка пропущена",
        }
    ok = is_playwright_version_compatible(installed)
    msg = (
        f"playwright {installed} OK (<{PLAYWRIGHT_MAX_VERSION_STR})"
        if ok
        else (
            f"playwright {installed} несовместим: требуется <{PLAYWRIGHT_MAX_VERSION_STR}. "
            f"Juggler отклоняет viewport.isMobile (см. {ISSUE_PLAYWRIGHT_653}). "
            f"Зафиксируйте: pip install 'playwright<1.61' (у нас 1.60.0)."
        )
    )
    return {
        "ok": ok,
        "status": "ok" if ok else "fail",
        "installed_version": installed,
        "required": f"<{PLAYWRIGHT_MAX_VERSION_STR}",
        "issue": ISSUE_PLAYWRIGHT_653,
        "message": msg,
    }


def assert_playwright_version() -> None:
    """Бросает RuntimeError если playwright >=1.61. Используется перед launch."""
    res = check_playwright_version()
    if not res["ok"]:
        raise RuntimeError(res["message"])


# ---------------------------------------------------------------------------
# 2. firefox_user_prefs handling
# ---------------------------------------------------------------------------

def validate_firefox_prefs_kwargs(kwargs: dict[str, Any]) -> list[str]:
    """
    Проверяет что префы браузера не попали в config по ошибке.
    Возвращает список предупреждений (пустой если всё ок).

    Правильно:
        Camoufox(..., config={"canvas:seed": ...}, firefox_user_prefs={"browser.cache...": ...})
    Неправильно:
        Camoufox(..., config={"browser.cache.disk.capacity": 51200})  # будет проигнорирован!
    """
    warnings_list: list[str] = []
    config = kwargs.get("config") or {}
    if isinstance(config, dict):
        for k in config:
            if _PREF_KEY_HINT in k:
                warnings_list.append(
                    f"Ключ '{k}' в config выглядит как firefox pref — должен быть в firefox_user_prefs, "
                    f"иначе будет проигнорирован Camoufox."
                )
    return warnings_list


def fix_camoufox_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """
    Авто-фикс: переносит ключи с точкой из config → firefox_user_prefs.
    Мутирует копию kwargs и возвращает её. Логирует warning при переносе.
    Идемпотентна — повторный вызов без новых ключей ничего не меняет.
    """
    fixed = dict(kwargs)
    config = fixed.get("config")
    if not isinstance(config, dict):
        return fixed
    to_move = [k for k in config if _PREF_KEY_HINT in k]
    if not to_move:
        return fixed
    prefs = fixed.get("firefox_user_prefs")
    if prefs is None:
        prefs = {}
        fixed["firefox_user_prefs"] = prefs
    elif not isinstance(prefs, dict):
        # неожиданный тип — не трогаем
        return fixed
    for k in to_move:
        if k not in prefs:
            prefs[k] = config.pop(k)
            warnings.warn(
                f"[patches] перенесён '{k}' из config → firefox_user_prefs (исправлено автоматически)",
                UserWarning,
                stacklevel=3,
            )
            logger.warning("перенесён '%s' из config → firefox_user_prefs", k)
    return fixed


# ---------------------------------------------------------------------------
# 3. Canvas/audio seed stability (beta.28 limitation)
# ---------------------------------------------------------------------------

def check_canvas_seed_limitation() -> dict[str, Any]:
    """
    Документирует ограничение beta.28: setCanvasSeed/setScreenDimensions
    отсутствуют в window → canvas/audio шум per-launch by design.

    Возвращает статус-документацию; не требует установленного camoufox.
    """
    return {
        "ok": True,
        "status": "warn",
        "binary": BETA_VERSION,
        "issue": ISSUE_CANVAS_SEED,
        "message": (
            f"Бинарь {BETA_VERSION}: window.setCanvasSeed отсутствует — canvas/audio шум "
            f"per-launch (детерминирован внутри сессии, различается между сессиями). "
            f"Это by design в stable, фича в ветках fingerprint-per-context. "
            f"Влияние минимально (см. ARCHITECTURE.md §7.1). "
            f"Наш костыль: детерминированные сиды + изоляция в session.py, "
            f"но реальный фикс — ждать мержа или патчить форк Level 3. Подробнее: {ISSUE_CANVAS_SEED}"
        ),
        "mitigation": "session.py задаёт config canvas:seed/audio:seed детерминированно; "
                      "под стабильный бинарь они станут полностью стабильны без доп. патча.",
    }


def wrap_canvas_seed_warning(page) -> bool:
    """
    Проверяет доступность setCanvasSeed в контексте страницы.
    Если отсутствует — логирует warning и возвращает False (ожидаемо на beta.28).
    Безопасен если page is None или evaluate падает.
    """
    if page is None:
        return False
    try:
        has = page.evaluate("() => typeof window.setCanvasSeed !== 'undefined' || typeof window.setScreenDimensions !== 'undefined'")
        if not has:
            warnings.warn(
                f"[patches] window.setCanvasSeed отсутствует (бинарь {BETA_VERSION}) — "
                f"canvas seed per-launch, см. {ISSUE_CANVAS_SEED}",
                UserWarning,
                stacklevel=3,
            )
            logger.warning("window.setCanvasSeed отсутствует — beta.28 per-launch шум")
            return False
        return bool(has)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 4. WebGL preset validation (пропуск битых альфа-пресетов)
# ---------------------------------------------------------------------------

def is_webgl_preset_valid(preset: dict[str, Any], os_name: str) -> bool:
    """
    Проверяет что WebGL-комбо пресета есть в bundled sample_webgl данных.
    Часть альфа-пресетов битая (vendor/renderer отсутствует в sample_webgl) — такие пропускаем
    детерминированным обходом в identity.resolve_fingerprint_preset.

    Работает без camoufox (fallback True), с camoufox — реальная проверка через sample_webgl.
    """
    webgl = (preset or {}).get("webgl") or {}
    vendor = webgl.get("unmaskedVendor")
    renderer = webgl.get("unmaskedRenderer")
    if not vendor or not renderer:
        return True  # нет webgl в пресете — ок, нечего валидировать
    try:
        from camoufox.webgl.sample import sample_webgl

        os_key = {"windows": "win", "macos": "mac", "linux": "lin"}.get(os_name, "win")
        sample_webgl(os_key, vendor, renderer)
        return True
    except ImportError:
        # camoufox не установлен — пропускаем проверку (graceful fallback)
        return True
    except Exception:
        return False


def find_valid_preset(presets: list[dict[str, Any]], os_name: str, start_index: int = 0) -> Optional[dict[str, Any]]:
    """
    Детерминированно обходит список пресетов с start_index, пропуская битые WebGL-комбо.
    Возвращает первый валидный или None если все битые (практически не случается).
    Используется как fallback если identity.resolve_fingerprint_preset недоступен.
    """
    if not presets:
        return None
    n = len(presets)
    for offset in range(n):
        candidate = presets[(start_index + offset) % n]
        if is_webgl_preset_valid(candidate, os_name):
            return candidate
    return None


# ---------------------------------------------------------------------------
# 5. Aggregated helpers: check_environment + apply_all
# ---------------------------------------------------------------------------

def check_environment() -> dict[str, Any]:
    """
    Агрегированная проверка окружения — все известные ограничения Camoufox.
    Никогда не бросает исключение; отсутствующий camoufox/playwright → skipped/warn.
    Возвращает dict с ключами playwright, canvas_seed, webgl, camoufox, overall.
    """
    playwright_res = check_playwright_version()
    canvas_res = check_canvas_seed_limitation()

    # WebGL — проверяем что модуль доступен
    try:
        import camoufox.webgl.sample  # noqa: F401

        webgl_res: dict[str, Any] = {"ok": True, "status": "ok", "message": "sample_webgl доступен"}
    except ImportError as e:
        webgl_res = {"ok": True, "status": "skipped", "message": f"camoufox не установлен — WebGL проверка пропущена: {e}"}
    except Exception as e:
        webgl_res = {"ok": False, "status": "warn", "message": str(e)[:200]}

    # Camoufox package availability
    try:
        from importlib.metadata import version as _pkg_version

        camo_v = _pkg_version("cloverlabs-camoufox")
        camo_res: dict[str, Any] = {"ok": True, "status": "ok", "version": camo_v}
    except Exception:
        try:
            import camoufox  # noqa: F401

            camo_res = {"ok": True, "status": "ok", "version": "unknown (importable)"}
        except Exception as e:
            camo_res = {"ok": True, "status": "skipped", "message": f"camoufox не установлен: {e}"}

    # overall: fail если playwright fail, иначе ok/warn
    if not playwright_res["ok"]:
        overall = "fail"
    elif canvas_res.get("status") == "warn":
        overall = "warn"
    else:
        overall = "ok"

    return {
        "playwright": playwright_res,
        "canvas_seed": canvas_res,
        "webgl": webgl_res,
        "camoufox": camo_res,
        "overall": overall,
        "beta_version": BETA_VERSION,
    }


def apply_all() -> dict[str, Any]:
    """
    Применяет все monkey-патчи (идемпотентно).
    Сейчас патчи — это проверки + фиксы kwargs + логирование; глубокая monkey-патча
    pythonlib добавляется по мере необходимости (см. PATCHES.md).

    Повторный вызов возвращает тот же отчёт без повторного патча.
    """
    global _PATCHED, _PATCH_REPORT
    if _PATCHED and _PATCH_REPORT is not None:
        return _PATCH_REPORT

    report: dict[str, Any] = {"patches": [], "warnings": []}

    # 1. Playwright guard — только проверка, не патчим рантайм
    pw = check_playwright_version()
    report["playwright"] = pw
    if not pw["ok"]:
        report["warnings"].append(pw["message"])
        logger.warning("%s", pw["message"])
    report["patches"].append("playwright_guard")

    # 2. firefox_user_prefs — сам фикс применяется в fix_camoufox_kwargs (вызывается из session.py)
    report["patches"].append("firefox_user_prefs_guard")

    # 3. Canvas seed limitation — документация + runtime check helper
    report["canvas_seed"] = check_canvas_seed_limitation()
    report["patches"].append("canvas_seed_documented")

    # 4. WebGL validation — helper доступен, глубокий патч не нужен (identity уже обходит)
    report["patches"].append("webgl_validation")

    # Попытка реального monkey-patch если camoufox установлен
    try:
        import camoufox.sync_api  # noqa: F401

        report["camoufox_patch"] = "available (no deep patch needed yet)"
    except Exception as e:
        report["camoufox_patch"] = f"skipped (camoufox not installed: {e})"

    report["patched"] = True
    report["idempotent"] = True

    _PATCHED = True
    _PATCH_REPORT = report
    logger.info("[patches] apply_all: %s", report["patches"])
    return report


# Алиас для импорта из session.py (требование задачи)
def apply_camoufox_patches() -> dict[str, Any]:
    """Алиас для apply_all — удобный импорт в session.py."""
    return apply_all()


def reset_patches_for_tests() -> None:
    """Сброс состояния патчей — только для тестов (идемпотентность)."""
    global _PATCHED, _PATCH_REPORT
    _PATCHED = False
    _PATCH_REPORT = None


__all__ = [
    "apply_all",
    "apply_camoufox_patches",
    "check_environment",
    "check_playwright_version",
    "is_playwright_version_compatible",
    "get_playwright_version",
    "assert_playwright_version",
    "validate_firefox_prefs_kwargs",
    "fix_camoufox_kwargs",
    "check_canvas_seed_limitation",
    "wrap_canvas_seed_warning",
    "is_webgl_preset_valid",
    "find_valid_preset",
    "reset_patches_for_tests",
    "PLAYWRIGHT_MAX_VERSION_STR",
    "ISSUE_PLAYWRIGHT_653",
    "BETA_VERSION",
]
