"""
Mouse — кривые Безье, джиттер, овершут. Для агента: page.click с humanize.
Если Camoufox humanize=False — делаем своё.
"""
from __future__ import annotations

import math
import random
import time
from typing import Any


def _bezier(p0: tuple[float, float], p1: tuple[float, float], p2: tuple[float, float], t: float) -> tuple[float, float]:
    x = (1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t**2 * p2[0]
    y = (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t**2 * p2[1]
    return x, y


def human_move(page: Any, x: float, y: float, steps: int = 25) -> None:
    """
    Ведёт мышь к (x,y) по кривой с джиттером. H6: энтропия + hover + scrollIntoView.
    """
    # H6: старт не из центра, а из рандомной точки (энтропия Kasada/PerimeterX)
    try:
        box = page.evaluate("() => ({x: window.innerWidth/2, y: window.innerHeight/2})")
        cx0, cy0 = float(box.get("x", 640)), float(box.get("y", 360))
        # рандомизируем старт: +/- 120px от центра с 70% вероятностью, иначе прямо из центра
        if random.random() < 0.7:
            sx = cx0 + random.uniform(-120, 120)
            sy = cy0 + random.uniform(-80, 80)
        else:
            sx, sy = cx0, cy0
        # clamp
        sx = max(0, min(sx, cx0 * 2))
        sy = max(0, min(sy, cy0 * 2))
    except Exception:
        sx, sy = 640 + random.uniform(-100, 100), 360 + random.uniform(-60, 60)

    # H6: scrollIntoView перед движением если элемент вне вьюпорта (add hover realism)
    try:
        # лёгкий hover перед движением — иногда делаем паузу как будто ищем элемент
        if random.random() < 0.15:
            time.sleep(random.uniform(0.08, 0.22))
    except Exception:
        pass

    # контрольная точка с рандомным смещением (увеличенная энтропия)
    cx = (sx + x) / 2 + random.uniform(-90, 90)
    cy = (sy + y) / 2 + random.uniform(-50, 50)
    # вторая контрольная для более естественной S-кривой (20% случаев)
    use_s_curve = random.random() < 0.20
    cx2 = (cx + x) / 2 + random.uniform(-30, 30) if use_s_curve else cx
    cy2 = (cy + y) / 2 + random.uniform(-20, 20) if use_s_curve else cy

    for i in range(steps):
        t = (i + 1) / steps
        # ease out quad
        t = 1 - math.pow(1 - t, 2.2)
        if use_s_curve:
            # кубическая Безье с 2 контролями (примерно)
            # аппроксимация через два квадратичных
            if t < 0.5:
                px, py = _bezier((sx, sy), (cx, cy), (cx2, cy2), t * 2)
            else:
                px, py = _bezier((cx2, cy2), (cx2, cy2), (x, y), (t - 0.5) * 2)
        else:
            px, py = _bezier((sx, sy), (cx, cy), (x, y), t)
        # H6: per-step pressure jitter (энтропия движения)
        px += random.gauss(0, 0.9)
        py += random.gauss(0, 0.9)
        # micro-pause variation
        try:
            page.mouse.move(px, py)
        except Exception:
            break
        # H6: variable timing per step (а не uniform 4-12ms) — Gauss 8±3ms
        try:
            d = max(0.003, min(0.018, random.gauss(0.008, 0.003)))
        except Exception:
            d = random.uniform(0.004, 0.012)
        time.sleep(d)

    # овершут 12% (было 10%)
    if random.random() < 0.12:
        try:
            page.mouse.move(x + random.uniform(2, 9), y + random.uniform(2, 9))
            time.sleep(random.uniform(0.04, 0.10))
            page.mouse.move(x, y)
        except Exception:
            pass
    # H6: hover 1.5% — случайный микродрейф после прибытия (как будто рука дрожит)
    if random.random() < 0.015:
        try:
            page.mouse.move(x + random.uniform(-1.5, 1.5), y + random.uniform(-1.5, 1.5))
            time.sleep(random.uniform(0.02, 0.06))
            page.mouse.move(x, y)
        except Exception:
            pass


def human_click(page: Any, selector: str, timeout: int = 10000) -> None:
    """
    Кликает по селектору с человеческим движением. H6: hover + scrollIntoView.
    """
    try:
        # H6: scrollIntoView перед кликом (как живой юзер)
        try:
            page.evaluate(f"""() => {{
                const el = document.querySelector('{selector}');
                if (el) el.scrollIntoView({{block: 'center', behavior: 'instant'}});
            }}""")
            time.sleep(random.uniform(0.12, 0.28))
        except Exception:
            pass
        box = page.locator(selector).first.bounding_box(timeout=timeout)
        if box:
            x = box["x"] + box["width"] / 2 + random.uniform(-3, 3)
            y = box["y"] + box["height"] / 2 + random.uniform(-3, 3)
            human_move(page, x, y)
            # H6: hover перед кликом 60% — пауза как будто прицеливается
            if random.random() < 0.60:
                time.sleep(random.uniform(0.06, 0.18))
            else:
                time.sleep(random.uniform(0.08, 0.22))
            # H6: keyDown pressure simulation — click с небольшим jitter
            try:
                page.mouse.down()
                time.sleep(random.uniform(0.04, 0.11))
                page.mouse.up()
            except Exception:
                page.mouse.click(x, y)
            return
    except Exception:
        pass
    # fallback: обычный click
    try:
        page.click(selector, timeout=timeout)
    except Exception:
        page.locator(selector).first.click(timeout=timeout)


def human_type(page: Any, selector: str, text: str, clear: bool = True) -> None:
    """
    Печатает как человек: вариативная задержка между символами, иногда пауза.
    H6: keyDown/keyUp pressure jitter (Kasada/PerimeterX смотрят на энтропию)
    — Gauss 105±35 + 10% micro-jitter + 4% long hold + per-key down/up timing.
    """
    try:
        loc = page.locator(selector).first
        # H6: hover + фокус с небольшой паузой
        try:
            loc.click(timeout=5000)
            time.sleep(random.uniform(0.06, 0.14))
        except Exception:
            loc.click(timeout=5000)
        if clear:
            try:
                page.keyboard.press("Control+A")
                time.sleep(random.uniform(0.05, 0.12))
                # иногда backspace после selectAll (более человечно)
                if random.random() < 0.30:
                    page.keyboard.press("Backspace")
                    time.sleep(random.uniform(0.04, 0.09))
            except Exception:
                pass
        # H6: per-character pressure model
        for ch in text:
            # base delay Gauss 105±35 clipped 45-180
            try:
                delay = int(max(45, min(180, random.gauss(105, 35))))
            except Exception:
                delay = random.randint(45, 180)
            # H6: 15% chance — press with explicit down/up + jitter (более энтропийно)
            if random.random() < 0.15:
                try:
                    page.keyboard.down(ch)
                    # keyDown pressure: hold 30-90ms Gauss
                    try:
                        hold = max(0.02, min(0.12, random.gauss(0.055, 0.018)))
                    except Exception:
                        hold = random.uniform(0.03, 0.09)
                    time.sleep(hold)
                    page.keyboard.up(ch)
                    # post-key delay
                    time.sleep(delay / 1000.0)
                except Exception:
                    page.keyboard.type(ch, delay=delay)
            else:
                page.keyboard.type(ch, delay=delay)
            # H6: long keyHold 4% + micro pressure jitter 10%
            if random.random() < 0.04:
                time.sleep(random.uniform(0.25, 0.65))
            if random.random() < 0.10:
                time.sleep(random.uniform(0.015, 0.04))
            # H6: occasional double-key hesitation 2% (опечатка-исправление симулируется паузой)
            if random.random() < 0.02:
                time.sleep(random.uniform(0.12, 0.28))
        time.sleep(random.uniform(0.15, 0.35))
    except Exception:
        try:
            page.fill(selector, text)
        except Exception:
            pass
