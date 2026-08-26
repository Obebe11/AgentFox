"""
Timing — гауссовы паузы, ритмы сессий. Человек никогда не спит ровно.
"""
from __future__ import annotations

import asyncio
import random
import time


def human_pause(mean: float = 3.0, std: float = 1.5, low: float = 0.5) -> float:
    v = random.gauss(mean, std)
    v = max(low, v)
    time.sleep(v)
    return v


async def ahuman_pause(mean: float = 3.0, std: float = 1.5, low: float = 0.5) -> float:
    v = max(low, random.gauss(mean, std))
    await asyncio.sleep(v)
    return v


def read_pause(content_length: int = 800) -> float:
    # ~200 слов/мин = 3.3 слова/сек, 5 симв ~ слово
    words = max(20, content_length / 5)
    base = words / 3.3
    # джиттер чтения + иногда длинная пауза
    v = random.gauss(base, base * 0.3)
    if random.random() < 0.08:
        v += random.uniform(15, 45)
    v = max(1.0, min(v, 60))
    time.sleep(v)
    return v


def jittered_interval(base: float, spread: float = 0.4) -> float:
    return max(0.2, random.gauss(base, base * spread))
