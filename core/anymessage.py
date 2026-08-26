r"""
Anymessage — обёртка для агента, чтобы не отвлекаться от задач.

Агент делает 1 вызов вместо 5:
  from core.anymessage import get_email, wait_code, wait_link

  email_id, email = get_email(site="x.com")  # site = куда регаешься
  # ... заполнил форму с email, нажал submit ...
  code = wait_code(email_id, timeout=120)  # ждёт письмо и парсит \b\d{6}\b
  link = wait_link(email_id)  # или ссылка confirm

Токен берётся из ENV ANYMESSAGE_TOKEN или файла tools/benchmark/anymessage.token
(не коммить token, он в .gitignore). Провайдер сам выберет домен с запасом.

Для бенча без сети — заглушка: если нет token → возвращает demo@example.com и SKIPPED.

Доки: https://anymessage.shop/en/docs , SDK anymessage-sdk 0.2.2
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Optional, Tuple

TOKEN = None
# 1) env
TOKEN = os.environ.get("ANYMESSAGE_TOKEN") or os.environ.get("ANYMESSAGE_SHOP_TOKEN")
# 2) files
if not TOKEN:
    for p in [
        Path(__file__).parent.parent / "tools" / "benchmark" / "anymessage.token",
        Path(__file__).parent.parent / "anymessage.token",
        Path.home() / ".config" / "anymessage" / "token",
        Path("/tmp/anymessage.token"),
    ]:
        if p.exists():
            try:
                TOKEN = p.read_text(encoding="utf-8").strip().split()[0]
                break
            except Exception:
                pass
# 3) no hardcode — token must be in file/env (user gave, stored in tools/benchmark/anymessage.token with 600)

def _client():
    try:
        from anymessage import AnyMessageClient
        return AnyMessageClient(TOKEN)
    except Exception as e:
        raise RuntimeError(f"anymessage-sdk not installed or token invalid: {e}") from e

def get_balance() -> float:
    """Баланс аккаунта, для проверки."""
    try:
        c = _client()
        b = c.get_balance()
        # b is float or BalanceResponse
        if hasattr(b, "balance"):
            return float(b.balance)
        return float(b)
    except Exception:
        return 0.0

def get_email(site: str = "x.com", domain: Optional[str] = None, subject: Optional[str] = None) -> Tuple[int, str]:
    """
    Заказывает почту под сайт (x.com, instagram.com, mail.ru и т.д.).
    Возвращает (id, email). id нужен для wait_code/wait_link/cancel.
    Без токена — демо.
    """
    if not TOKEN or TOKEN == "demo":
        return 0, "demo@example.com"
    c = _client()
    # domain aggregator: если не указан, пусть API выберет
    # для x.com лучше rambler.ru / yandex.ru (дешевле, есть сток)
    kwargs = {"site": site}
    if domain:
        kwargs["domain"] = domain
    if subject:
        kwargs["subject"] = subject
    # choose cheap with stock if domain not given
    if not domain:
        if site == "x.com":
            kwargs["domain"] = "rambler.ru,yandex.ru,ya.ru,icloud.com"
        elif site in ("instagram.com", "mail.ru"):
            kwargs["domain"] = "rambler.ru,yandex.ee,yandex.kz"
    order = c.order_email(**kwargs)
    # order has id, email
    oid = getattr(order, "id", None) or getattr(order, "email_id", None) or 0
    em = getattr(order, "email", "") or getattr(order, "address", "") or ""
    return int(oid), str(em)

def wait_code(email_id: int, regex: str = r"\b\d{6}\b", timeout: int = 120, poll: int = 3) -> Optional[str]:
    """Ждёт письмо и вытаскивает код по regex. None если таймаут."""
    if email_id == 0 or not TOKEN:
        return None
    c = _client()
    try:
        msg = c.wait_for_message(id=email_id, timeout=timeout, poll_interval=poll)
        html = getattr(msg, "html", "") or getattr(msg, "text", "") or str(msg)
        m = re.search(regex, html)
        return m.group(0) if m else None
    except Exception:
        return None

def wait_link(email_id: int, regex: str = r"https?://[^\s\"']+confirm[^\s\"']+", timeout: int = 120) -> Optional[str]:
    """Ждёт письмо и вытаскивает ссылку подтверждения."""
    return wait_code(email_id, regex=regex, timeout=timeout)

def order_wait_and_extract(site: str, regex: str = r"\b\d{6}\b", timeout: int = 120, domain: Optional[str] = None) -> Tuple[int, str, Optional[str]]:
    """
    1 вызов = заказ + ожидание + парс. Удобно для агента.
    Возвращает (id, email, match_or_None). Не отвлекайся на детали.
    """
    oid, email = get_email(site=site, domain=domain)
    if oid == 0:
        return oid, email, None
    match = wait_code(oid, regex=regex, timeout=timeout)
    return oid, email, match

def cancel(email_id: int) -> bool:
    if email_id == 0 or not TOKEN:
        return False
    try:
        _client().cancel_email(id=email_id)
        return True
    except Exception:
        return False

def wait_for_message_simple(email_id: int, timeout: int = 120) -> Optional[str]:
    """Вернуть весь HTML письма (для отладки)."""
    if email_id == 0:
        return None
    try:
        c = _client()
        msg = c.wait_for_message(id=email_id, timeout=timeout)
        return getattr(msg, "html", "") or str(msg)
    except Exception:
        return None

# --- удобные алиасы для AGENT.md ---
# from core.anymessage import get_email_for_x, get_email_for_instagram
def get_email_for_x() -> Tuple[int, str]:
    return get_email(site="x.com")

def get_email_for_instagram() -> Tuple[int, str]:
    return get_email(site="instagram.com")

def get_email_for_mailru() -> Tuple[int, str]:
    return get_email(site="mail.ru")
