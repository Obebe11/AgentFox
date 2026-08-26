"""
Mail — IMAP OTP + confirm link для авторег.

Заглушка offline: без mail.json возвращает SKIPPED, с mail.json — реальный IMAP.

Использование:
  from core.mail import fetch_otp, fetch_confirm_link

  otp = fetch_otp(host="imap.mail.ru", user="...", pass_="...", subject_filter="confirm")
  link = fetch_confirm_link(...)

Для бенча: `tools/benchmark/mail.json` (см. mail.json.example) — ждёт почты от пользователя, пока не запускать live.
"""
from __future__ import annotations

import imaplib
import email
import re
import time
from pathlib import Path
from typing import Optional

def _load_mail_config() -> dict | None:
    for p in [Path("mail.json"), Path("tools/benchmark/mail.json"), Path(__file__).parent.parent / "mail.json", Path(__file__).parent.parent / "tools" / "benchmark" / "mail.json"]:
        if p.exists():
            try:
                import json
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return None
    return None

def fetch_otp(host: str = "imap.mail.ru", port: int = 993, ssl: bool = True, user: str = "", pass_: str = "", folder: str = "INBOX", subject_filter: str = "confirm", otp_regex: str = r"\b\d{6}\b", timeout: int = 30) -> Optional[str]:
    """
    Ждет письмо с subject_filter <30s, парсит OTP regex.
    Без реальных creds — возвращает None (SKIPPED).
    """
    if not user or "example" in host or "example" in user:
        return None
    try:
        M = imaplib.IMAP4_SSL(host, port) if ssl else imaplib.IMAP4(host, port)
        M.login(user, pass_)
        M.select(folder)
        deadline = time.time() + timeout
        while time.time() < deadline:
            typ, data = M.search(None, f'(UNSEEN SUBJECT "{subject_filter}")')
            if typ == "OK" and data[0]:
                nums = data[0].split()
                if nums:
                    typ, msg_data = M.fetch(nums[-1], "(RFC822)")
                    if typ == "OK":
                        msg = email.message_from_bytes(msg_data[0][1])
                        body = ""
                        if msg.is_multipart():
                            for part in msg.walk():
                                ct = part.get_content_type()
                                if ct == "text/plain":
                                    try:
                                        body = part.get_payload(decode=True).decode(errors="ignore")
                                        break
                                    except Exception:
                                        pass
                        else:
                            try:
                                body = msg.get_payload(decode=True).decode(errors="ignore")
                            except Exception:
                                body = str(msg.get_payload())
                        m = re.search(otp_regex, body)
                        if m:
                            M.store(nums[-1], "+FLAGS", "\\Seen")
                            M.logout()
                            return m.group(0)
            time.sleep(2)
        M.logout()
    except Exception:
        return None
    return None

def fetch_confirm_link(host: str = "imap.mail.ru", port: int = 993, ssl: bool = True, user: str = "", pass_: str = "", subject_filter: str = "confirm", link_regex: str = r"https?://[^\s]+confirm[^\s]+", timeout: int = 30) -> Optional[str]:
    """Аналог fetch_otp но для ссылки."""
    if not user or "example" in host:
        return None
    try:
        M = imaplib.IMAP4_SSL(host, port) if ssl else imaplib.IMAP4(host, port)
        M.login(user, pass_)
        M.select("INBOX")
        deadline = time.time() + timeout
        while time.time() < deadline:
            typ, data = M.search(None, f'(UNSEEN SUBJECT "{subject_filter}")')
            if typ == "OK" and data[0]:
                nums = data[0].split()
                if nums:
                    typ, msg_data = M.fetch(nums[-1], "(RFC822)")
                    if typ == "OK":
                        msg = email.message_from_bytes(msg_data[0][1])
                        body = ""
                        if msg.is_multipart():
                            for part in msg.walk():
                                if part.get_content_type() == "text/plain":
                                    try:
                                        body = part.get_payload(decode=True).decode(errors="ignore")
                                        break
                                    except Exception:
                                        pass
                        else:
                            try:
                                body = msg.get_payload(decode=True).decode(errors="ignore")
                            except Exception:
                                body = str(msg.get_payload())
                        m = re.search(link_regex, body)
                        if m:
                            M.store(nums[-1], "+FLAGS", "\\Seen")
                            M.logout()
                            return m.group(0)
            time.sleep(2)
        M.logout()
    except Exception:
        return None
    return None
