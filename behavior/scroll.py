"""
Scroll — инерционный скролл, переменный шаг, иногда назад.
"""
from __future__ import annotations

import random
import time
from typing import Any


def natural_scroll(page: Any, screens: int = 2, depth: str = "light") -> None:
    """
    Скроллит как человек: переменный шаг, паузы, иногда откат вверх.
    depth: light | deep
    Fallback: если wheel не двинул scrollY (headless data:), делает evaluate scrollBy.
    """
    total = random.randint(800, 2000) if depth == "light" else random.randint(2000, 5000)
    scrolled = 0
    while scrolled < total:
        step = random.randint(180, 520)
        y_before = 0
        try:
            y_before = page.evaluate("() => window.scrollY") if hasattr(page, "evaluate") else 0
        except Exception:
            y_before = 0
        moved = False
        try:
            page.mouse.wheel(0, step)
            moved = True
        except Exception:
            pass
        # если wheel не сдвинул страницу — fallback на JS (реальный браузер все равно)
        if moved:
            try:
                y_after = page.evaluate("() => window.scrollY") if hasattr(page, "evaluate") else y_before + step
                if y_after == y_before:
                    page.evaluate(f"window.scrollBy(0,{step})")
            except Exception:
                try:
                    page.evaluate(f"window.scrollBy(0,{step})")
                except Exception:
                    pass
        else:
            try:
                page.evaluate(f"window.scrollBy(0,{step})")
            except Exception:
                break
        scrolled += step
        time.sleep(random.uniform(0.25, 0.85))
        if random.random() < 0.12:
            back = random.randint(80, 260)
            try:
                page.mouse.wheel(0, -back)
                # проверяем откат
                try:
                    y2 = page.evaluate("() => window.scrollY")
                    y1 = y_after if 'y_after' in locals() else y2 + back
                    if y2 == y1:
                        page.evaluate(f"window.scrollBy(0,{-back})")
                except Exception:
                    pass
            except Exception:
                try:
                    page.evaluate(f"window.scrollBy(0,{-back})")
                except Exception:
                    pass
            time.sleep(random.uniform(0.4, 1.0))
        if random.random() < 0.07:
            time.sleep(random.uniform(1.2, 3.0))


async def anatural_scroll(page: Any, screens: int = 2, depth: str = "light") -> None:
    import asyncio

    total = random.randint(800, 2000) if depth == "light" else random.randint(2000, 5000)
    scrolled = 0
    while scrolled < total:
        step = random.randint(180, 520)
        try:
            page.mouse.wheel(0, step)
        except Exception:
            try:
                await page.evaluate(f"window.scrollBy(0,{step})")
            except Exception:
                break
        scrolled += step
        await asyncio.sleep(random.uniform(0.25, 0.85))
        if random.random() < 0.12:
            back = random.randint(80, 260)
            try:
                page.mouse.wheel(0, -back)
            except Exception:
                try:
                    await page.evaluate(f"window.scrollBy(0,{-back})")
                except Exception:
                    pass
            await asyncio.sleep(random.uniform(0.4, 1.0))
