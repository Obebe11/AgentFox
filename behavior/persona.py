"""
Persona — паттерны чтения, разнообразие маршрутов, «отвлечения».
"""
from __future__ import annotations

import random
from typing import Any

from .scroll import natural_scroll
from .timing import human_pause, read_pause


def warmup_visit(page: Any) -> None:
    """Лёгкий визит для прогрева: скролл + пауза как будто читает."""
    human_pause(2.5, 1.2)
    natural_scroll(page, depth="light")
    # оценить длину контента и «почитать»
    try:
        length = page.evaluate("document.body.innerText.length") or 800
    except Exception:
        length = 800
    read_pause(int(length))


def maybe_detour(page: Any, p: float = 0.35) -> bool:
    """
    С вероятностью p делает «отвлечение» — скролл вверх, пауза, возврат.
    Возвращает True если сделал.
    """
    if random.random() > p:
        return False
    # скролл вверх
    try:
        page.mouse.wheel(0, -random.randint(200, 500))
    except Exception:
        try:
            page.evaluate("window.scrollBy(0,-300)")
        except Exception:
            return False
    human_pause(1.2, 0.6)
    # лёгкий скролл вниз обратно
    natural_scroll(page, depth="light")
    return True
