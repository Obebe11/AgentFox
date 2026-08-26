"""
crypto — шифрование meta.json at-rest (H9).

Использует Fernet (cryptography) если доступна, иначе fallback к plain.
Ключ: env AGENTFOX_MASTER_KEY (base64 32 bytes) или AGENTFOX_ENCRYPTION_KEY.
Если ключей нет — plain (обратно совместимо). Если ключ задан — шифрует.

Формат шифрованного файла: b'gAAAA...' (Fernet token) — детектится по префиксу.
Plain: JSON текст starting with '{'.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any

_FERNET_PREFIX = b"gAAAA"


def _get_key() -> bytes | None:
    raw = os.getenv("AGENTFOX_MASTER_KEY") or os.getenv("AGENTFOX_ENCRYPTION_KEY") or ""
    raw = raw.strip()
    if not raw:
        return None
    # support both raw base64 44 chars and hex
    try:
        # Fernet expects 32 urlsafe base64-encoded bytes (44 chars with padding)
        # If user gave hex, convert
        if len(raw) == 64 and all(c in "0123456789abcdefABCDEF" for c in raw):
            raw_bytes = bytes.fromhex(raw)
            return base64.urlsafe_b64encode(raw_bytes)
        # if already base64 urlsafe, validate
        decoded = base64.urlsafe_b64decode(raw + "==" if len(raw) % 4 else raw)
        if len(decoded) == 32:
            # re-encode canonical
            return base64.urlsafe_b64encode(decoded)
        # try standard base64
        decoded2 = base64.b64decode(raw)
        if len(decoded2) == 32:
            return base64.urlsafe_b64encode(decoded2)
    except Exception:
        pass
    # assume already Fernet key string (44 chars)
    try:
        # Try to create Fernet to validate
        from cryptography.fernet import Fernet

        Fernet(raw.encode())  # validation
        return raw.encode()
    except Exception:
        pass
    return None


def _get_fernet():
    key = _get_key()
    if key is None:
        return None
    try:
        from cryptography.fernet import Fernet

        return Fernet(key)
    except ImportError:
        return None
    except Exception:
        return None


def is_encrypted(data: bytes) -> bool:
    return data.startswith(_FERNET_PREFIX)


def encrypt_json(data: dict) -> bytes:
    """Сериализует dict -> bytes, шифрует если ключ задан."""
    plain = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    f = _get_fernet()
    if f is None:
        return plain
    return f.encrypt(plain)


def decrypt_bytes(raw: bytes) -> dict:
    """Дешифрует bytes -> dict. Авто-детект plain vs encrypted."""
    raw = raw.strip()
    if is_encrypted(raw):
        f = _get_fernet()
        if f is None:
            raise RuntimeError("meta.json encrypted but AGENTFOX_MASTER_KEY not set or cryptography not installed")
        plain = f.decrypt(raw)
        return json.loads(plain.decode("utf-8"))
    # plain JSON
    return json.loads(raw.decode("utf-8"))


def generate_key() -> str:
    """Генерирует новый Fernet ключ (base64 urlsafe 44 chars)."""
    try:
        from cryptography.fernet import Fernet

        return Fernet.generate_key().decode()
    except ImportError:
        import secrets

        return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()


def is_encryption_enabled() -> bool:
    return _get_fernet() is not None


def encrypt_file(path: Path) -> bool:
    """Шифрует существующий plain meta.json на месте. Returns True if encrypted."""
    if not path.exists():
        return False
    raw = path.read_bytes()
    if is_encrypted(raw):
        return False
    f = _get_fernet()
    if f is None:
        return False
    data = json.loads(raw.decode("utf-8"))
    enc = f.encrypt(json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(enc)
    tmp.replace(path)
    return True


def decrypt_file(path: Path) -> bool:
    """Дешифрует на месте если зашифрован. Requires key."""
    if not path.exists():
        return False
    raw = path.read_bytes()
    if not is_encrypted(raw):
        return False
    f = _get_fernet()
    if f is None:
        raise RuntimeError("cannot decrypt without key")
    data = decrypt_bytes(raw)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return True
