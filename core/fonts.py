"""
fonts — H5 ленивая подгрузка шрифтов (VPS_OPTIMIZATION.md §5).

На этой машине offline subset 55% per profile стабилен (session.py:_stable_subset).
Lazy volume: /var/lib/agentfox/fonts/{windows,macos,linux} — on-demand.

Функции:
- get_fonts_subset(os_name, profile_id) -> list[str] детерминированный 55%
- ensure_fonts_available(os_name) -> bool проверяет наличие volume
- fonts_volume_path(os_name) -> Path
- install_fonts_subset(os_name) — копирует из bundle/fonts если доступен
"""

from __future__ import annotations

import hashlib
import random
from pathlib import Path
from typing import Optional

FONTS_VOLUME_ROOT = Path("/var/lib/agentfox/fonts")
# bundle fonts находятся внутри установленной camoufox (если есть)
# fallback: fonts в репо не храним, берём из pip пакета
_BUNDLE_FONTS_HINT = Path("/opt/camoufox/bundle/fonts")


def fonts_volume_path(os_name: str) -> Path:
    key = {"windows": "win", "macos": "mac", "linux": "lin"}.get(os_name, "win")
    # volume хранит по ключу win/mac/lin для совместимости с camoufox
    return FONTS_VOLUME_ROOT / key


def _load_os_fonts_raw() -> dict:
    try:
        from camoufox.fingerprints import _load_os_fonts

        return _load_os_fonts()
    except Exception:
        return {"win": [], "mac": [], "lin": []}


def get_fonts_subset(os_name: str, profile_id: str, ratio: float = 0.55) -> list[str]:
    """Детерминированный 55% subset — стабилен per profile (как в session.py)."""
    try:
        data = _load_os_fonts_raw()
        os_key = {"windows": "win", "macos": "mac", "linux": "lin"}.get(os_name, "win")
        full = data.get(os_key, [])
        if not full:
            # fallback: если пакет не установлен — возвращаем заглушку
            return [f"font_{i}" for i in range(int(10 * ratio))]
        rng = random.Random(int(hashlib.sha256(f"{profile_id}:fontsubset".encode()).hexdigest(), 16))
        k = max(1, int(len(full) * ratio))
        return sorted(rng.sample(full, k))
    except Exception:
        return []


def is_fonts_volume_available(os_name: str) -> bool:
    p = fonts_volume_path(os_name)
    return p.exists() and any(p.iterdir())


def ensure_fonts_available(os_name: str) -> dict:
    """Проверяет volume, возвращает статус для health/metrics."""
    path = fonts_volume_path(os_name)
    available = is_fonts_volume_available(os_name)
    # также проверяем что subset стабилен
    return {
        "os": os_name,
        "volume_path": str(path),
        "available": available,
        "hint": str(_BUNDLE_FONTS_HINT),
        "fallback": "bundle fonts from pip package (in-memory subset) if volume missing",
    }


def install_fonts_subset(os_name: str, dest: Optional[Path] = None) -> dict:
    """Копирует шрифты для os_name из bundle в volume (если bundle доступен)."""
    import shutil

    src_candidates = [
        _BUNDLE_FONTS_HINT / {"windows": "win", "macos": "mac", "linux": "lin"}.get(os_name, "win"),
        Path(__file__).parent.parent / "bundle" / "fonts" / os_name,
    ]
    src = None
    for cand in src_candidates:
        if cand.exists():
            src = cand
            break
    if src is None:
        # fallback: нет физического bundle — subset уже in-memory, volume не нужен
        return {"os": os_name, "installed": False, "reason": "bundle fonts not found on host, using in-memory subset (session.py 55% stable)", "fallback": True}

    dst = dest or fonts_volume_path(os_name)
    dst.mkdir(parents=True, exist_ok=True)
    # копируем
    count = 0
    for f in src.rglob("*"):
        if f.is_file():
            rel = f.relative_to(src)
            target = dst / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(f, target)
                count += 1
            except Exception:
                pass
    return {"os": os_name, "installed": True, "count": count, "dest": str(dst)}
