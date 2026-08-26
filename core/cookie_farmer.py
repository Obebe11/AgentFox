"""
Cookie Farmer — аналог Cookie Robot (Multilogin).

Новый профиль со стерильными куками = бот. Реальный браузер имеет сотни
сторонних кук (_ga, _fbp, __utm...). Фарм-пул накапливает реалистичные наборы
по гео/локали и засевает новый профиль до первой сессии.
"""
from __future__ import annotations

import json
import random
import time
from pathlib import Path
from typing import Optional

from .profile_manager import Profile

# Топ-сайты для фарма по гео — дают реалистичные сторонние куки (по 20 на гео)
FARM_URLS_BY_GEO: dict[str, list[str]] = {
    "DE": [
        "https://www.google.de", "https://www.youtube.com", "https://www.zeit.de", "https://www.spiegel.de",
        "https://www.bild.de", "https://www.faz.net", "https://www.welt.de", "https://www.amazon.de",
        "https://www.ebay.de", "https://www.otto.de", "https://www.zalando.de", "https://www.idealo.de",
        "https://www.web.de", "https://www.gmx.net", "https://www.t-online.de", "https://www.focus.de",
        "https://www.stern.de", "https://www.chip.de", "https://www.heise.de", "https://www.n-tv.de",
    ],
    "US": [
        "https://www.google.com", "https://www.youtube.com", "https://www.nytimes.com", "https://www.reddit.com",
        "https://www.amazon.com", "https://www.ebay.com", "https://www.facebook.com", "https://www.instagram.com",
        "https://www.x.com", "https://www.linkedin.com", "https://www.cnn.com", "https://www.washingtonpost.com",
        "https://www.wsj.com", "https://www.yahoo.com", "https://www.walmart.com", "https://www.etsy.com",
        "https://www.pinterest.com", "https://www.twitch.tv", "https://www.espn.com", "https://www.imdb.com",
    ],
    "FR": [
        "https://www.google.fr", "https://www.youtube.com", "https://www.lemonde.fr", "https://www.lefigaro.fr",
        "https://www.amazon.fr", "https://www.ebay.fr", "https://www.fnac.com", "https://www.cdiscount.com",
        "https://www.orange.fr", "https://www.bfmtv.com", "https://www.20minutes.fr", "https://www.ouest-france.fr",
        "https://www.leparisien.fr", "https://www.allocine.fr", "https://www.seloger.com", "https://www.marmiton.org",
        "https://www.doctissimo.fr", "https://www.ladepeche.fr", "https://www.sudouest.fr", "https://www.programme-tv.net",
    ],
    "GB": [
        "https://www.google.co.uk", "https://www.youtube.com", "https://www.bbc.co.uk", "https://www.amazon.co.uk",
        "https://www.ebay.co.uk", "https://www.theguardian.com", "https://www.dailymail.co.uk", "https://www.telegraph.co.uk",
        "https://www.independent.co.uk", "https://www.thesun.co.uk", "https://www.argos.co.uk", "https://www.tesco.com",
        "https://www.sky.com", "https://www.metro.co.uk", "https://www.standard.co.uk", "https://www.gov.uk",
        "https://www.rightmove.co.uk", "https://www.booking.com", "https://www.asos.com", "https://www.bbc.com",
    ],
    "_default": [
        "https://www.google.com", "https://www.youtube.com", "https://www.wikipedia.org", "https://www.facebook.com",
        "https://www.instagram.com", "https://www.amazon.com", "https://www.reddit.com", "https://www.x.com",
        "https://www.linkedin.com", "https://www.tiktok.com", "https://www.yahoo.com", "https://www.bing.com",
        "https://www.ebay.com", "https://www.cnn.com", "https://www.bbc.com", "https://www.nytimes.com",
        "https://www.espn.com", "https://www.imdb.com", "https://www.twitch.tv", "https://www.booking.com",
    ],
}

BANK_DIR = Path(__file__).parent.parent / "profiles" / "_cookie_bank"
BANK_DIR.mkdir(parents=True, exist_ok=True)


def _bank_path(geo: str, locale: str) -> Path:
    return BANK_DIR / f"{geo}_{locale}.json"


def farm_profile(profile: Profile, urls: Optional[list[str]] = None, headless: bool = True) -> dict:
    """
    Прогоняет профиль по фарм-URL чтобы нагулять куки.
    Вызывать до warmup stage 1. Экономит 5–7 дней стерильности.
    """
    geo = profile.proxy.geo if profile.proxy else "DE"
    locale = profile.identity.locale
    todo = urls or FARM_URLS_BY_GEO.get(geo, FARM_URLS_BY_GEO["_default"])

    from .session import get_engine

    engine = get_engine(profile)
    stats: dict = {"visited": 0, "errors": []}
    try:
        page = engine.launch(profile, headless=headless)
        # Гейт stage 1 разрешает только browse/read — используем их
        ok, reason = profile.warmup.is_allowed("browse"), ""
        if not profile.warmup.is_allowed("browse"):
            # фарм всё равно разрешаем — это часть прогрева
            pass
        for url in todo[:4]:  # 4 сайта достаточно для ~30–60 кук
            try:
                page.goto(url, timeout=30000, wait_until="domcontentloaded")
                # дать трекерам отработать
                time.sleep(random.uniform(4, 8))
                # лёгкий скролл чтобы догрузить ленивые трекеры
                try:
                    page.evaluate("window.scrollBy(0, 600)")
                    time.sleep(random.uniform(1.5, 3.0))
                except Exception:
                    pass
                stats["visited"] += 1
                time.sleep(random.uniform(2, 5))
            except Exception as e:
                stats["errors"].append(f"{url}: {e}")
        # Сохранить куки в банк
        try:
            cookies = page.context.cookies() if hasattr(page, "context") else []
            # page.context может быть у Camoufox page.context
            bank = _bank_path(geo, locale)
            existing = json.loads(bank.read_text()) if bank.exists() else []
            existing.extend(cookies)
            # дедуп по name+domain
            seen: set[tuple[str, str]] = set()
            uniq: list[dict] = []
            for c in existing:
                k = (c.get("name", ""), c.get("domain", ""))
                if k not in seen:
                    seen.add(k)
                    uniq.append(c)
            bank.write_text(json.dumps(uniq[-200:], ensure_ascii=False, indent=2))
            stats["bank_size"] = len(uniq)
        except Exception as e:
            stats["errors"].append(f"bank: {e}")
    finally:
        try:
            engine.close()
        except Exception:
            pass
    return stats


def seed_from_bank(profile: Profile) -> int:
    """
    Засевает новый профиль куками из банка по совпадению geo/locale.
    Возвращает сколько кук засидил. Вызывать до первой сессии.
    """
    geo = profile.proxy.geo if profile.proxy else "DE"
    locale = profile.identity.locale
    bank = _bank_path(geo, locale)
    if not bank.exists():
        # fallback на _default
        bank = BANK_DIR / f"_default_{locale}.json"
        if not bank.exists():
            return 0
    try:
        cookies = json.loads(bank.read_text())
        # инъекция через Playwright context.add_cookies требует запущенного контекста
        # Для MVP сохраняем в user_data как Netscape cookies.txt — Camoufox подхватит при старте
        # Упрощённо: кладём рядом cookie_seed.json, session.py подхватит при launch
        seed_path = profile.dir / "cookie_seed.json"
        seed_path.write_text(json.dumps(cookies[:80], ensure_ascii=False, indent=2))
        return min(len(cookies), 80)
    except Exception:
        return 0
