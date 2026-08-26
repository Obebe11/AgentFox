"""
cloud_sync — Cloud Sync + шифрование (P1, ADS Power cross-device).

Делает то же что ADS Power cloud sync, но для AgentFox:
- экспорт профиля в tar.zst (profile_io) + шифрование Fernet (crypto)
- загрузка в S3 (или локальный volume как fallback)
- скачивание + импорт

Если boto3 не установлен или S3 creds не заданы — fallback к локальному
синк-директорию profiles/.cloud/ (имитирует S3 для тестов/VPS без S3).

Env:
- AGENTFOX_S3_BUCKET, AGENTFOX_S3_PREFIX, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
- AGENTFOX_CLOUD_DIR (fallback local dir, default profiles/.cloud)
- AGENTFOX_S3_REGION (default us-east-1)
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .profile_manager import PROFILES_ROOT

CLOUD_DIR_DEFAULT = PROFILES_ROOT / ".cloud"


def _cloud_dir() -> Path:
    p = Path(os.getenv("AGENTFOX_CLOUD_DIR", str(CLOUD_DIR_DEFAULT)))
    p.mkdir(parents=True, exist_ok=True)
    return p


def _s3_config() -> dict | None:
    bucket = os.getenv("AGENTFOX_S3_BUCKET", "").strip()
    if not bucket:
        return None
    return {
        "bucket": bucket,
        "prefix": os.getenv("AGENTFOX_S3_PREFIX", "agentfox").strip().strip("/"),
        "region": os.getenv("AGENTFOX_S3_REGION", "us-east-1").strip() or "us-east-1",
        "endpoint": os.getenv("AGENTFOX_S3_ENDPOINT", "").strip() or None,
    }


def _has_boto3() -> bool:
    try:
        import boto3  # noqa: F401

        return True
    except ImportError:
        return False


def _s3_client():
    import boto3

    cfg = _s3_config()
    if not cfg:
        return None
    kwargs: dict = {"region_name": cfg["region"]}
    if cfg["endpoint"]:
        kwargs["endpoint_url"] = cfg["endpoint"]
    return boto3.client("s3", **kwargs)


def _cloud_key(pid: str) -> str:
    cfg = _s3_config()
    prefix = cfg["prefix"] if cfg else "agentfox"
    # шифрованный архив имеет .tar.zst или .tar.zst.enc
    # если шифрование включено — добавляем .enc
    from .crypto import is_encryption_enabled

    suffix = ".tar.zst.enc" if is_encryption_enabled() else ".tar.zst"
    return f"{prefix}/{pid}{suffix}" if prefix else f"{pid}{suffix}"


def _local_path(pid: str) -> Path:
    from .crypto import is_encryption_enabled

    suffix = ".tar.zst.enc" if is_encryption_enabled() else ".tar.zst"
    return _cloud_dir() / f"{pid}{suffix}"


def push_profile(pid: str) -> dict:
    """Экспорт + загрузка в облако. Возвращает {cloud_key, local_path, bytes, s3}."""
    from .profile_io import export_profile_bytes

    data = export_profile_bytes(pid)
    # если шифрование включено — export_profile_bytes уже шифрует meta.json внутри tar,
    # но сам tar остаётся plain. Для defense-in-depth шифруем весь архив если ключ задан
    from .crypto import is_encryption_enabled

    if is_encryption_enabled():
        try:
            from cryptography.fernet import Fernet
            import os as _os

            key = _os.getenv("AGENTFOX_MASTER_KEY") or _os.getenv("AGENTFOX_ENCRYPTION_KEY")
            if key:
                # ensure Fernet key format
                from .crypto import _get_fernet

                f = _get_fernet()
                if f:
                    data = f.encrypt(data)
        except Exception:
            pass

    cfg = _s3_config()
    if cfg and _has_boto3():
        client = _s3_client()
        key = _cloud_key(pid)
        try:
            client.put_object(Bucket=cfg["bucket"], Key=key, Body=data, ContentType="application/octet-stream")
            return {"pid": pid, "cloud_key": key, "bytes": len(data), "backend": "s3", "bucket": cfg["bucket"]}
        except Exception as e:
            # fallback to local
            lp = _local_path(pid)
            lp.write_bytes(data)
            return {"pid": pid, "cloud_key": key, "bytes": len(data), "backend": "local_fallback", "local_path": str(lp), "error": str(e)[:200]}
    else:
        lp = _local_path(pid)
        lp.write_bytes(data)
        return {"pid": pid, "bytes": len(data), "backend": "local", "local_path": str(lp), "cloud_key": _cloud_key(pid)}


def pull_profile(pid: str, new_id: str | None = None, overwrite: bool = False) -> dict:
    """Скачивание из облака + импорт. Возвращает Profile.to_dict()."""
    from .profile_io import import_profile_bytes

    cfg = _s3_config()
    data: bytes | None = None
    backend = "local"
    if cfg and _has_boto3():
        client = _s3_client()
        key = _cloud_key(pid)
        try:
            resp = client.get_object(Bucket=cfg["bucket"], Key=key)
            data = resp["Body"].read()
            backend = "s3"
        except Exception:
            pass
    if data is None:
        # fallback local
        lp = _local_path(pid)
        # также пробуем альтернативный suffix (enc vs plain)
        if not lp.exists():
            alt = _cloud_dir() / f"{pid}.tar.zst"
            alt2 = _cloud_dir() / f"{pid}.tar.zst.enc"
            for cand in (alt, alt2, lp):
                if cand.exists():
                    lp = cand
                    break
        if not lp.exists():
            raise FileNotFoundError(f"cloud archive for {pid} not found (local {lp} and s3)")
        data = lp.read_bytes()

    # если архив зашифрован целиком — дешифруем
    if data[:5] == b"gAAAA":
        try:
            from .crypto import _get_fernet

            f = _get_fernet()
            if f:
                data = f.decrypt(data)
            else:
                raise RuntimeError("cloud archive encrypted but AGENTFOX_MASTER_KEY not set")
        except Exception as e:
            raise RuntimeError(f"decrypt failed: {e}")

    prof = import_profile_bytes(data, new_id=new_id, overwrite=overwrite)
    return {"pid": prof.id, "profile": prof.to_dict(), "backend": backend, "bytes": len(data)}


def list_cloud() -> list[dict]:
    """Листинг облака (S3 или local)."""
    cfg = _s3_config()
    if cfg and _has_boto3():
        client = _s3_client()
        try:
            resp = client.list_objects_v2(Bucket=cfg["bucket"], Prefix=cfg["prefix"] + "/" if cfg["prefix"] else "")
            out = []
            for obj in resp.get("Contents", []):
                out.append({"key": obj["Key"], "size": obj["Size"], "last_modified": obj["LastModified"].isoformat() if hasattr(obj["LastModified"], "isoformat") else str(obj["LastModified"])})
            return out
        except Exception:
            pass
    # local
    d = _cloud_dir()
    out = []
    for p in sorted(d.iterdir()):
        if p.is_file() and p.suffix in (".zst", ".gz", ".enc", ".tar"):
            out.append({"key": p.name, "size": p.stat().st_size, "local_path": str(p), "backend": "local"})
    # также .tar.zst double suffix
    for p in sorted(d.glob("*.tar.*")):
        if str(p) not in [o.get("local_path") for o in out]:
            out.append({"key": p.name, "size": p.stat().st_size, "local_path": str(p), "backend": "local"})
    return out


def delete_cloud(pid: str) -> bool:
    cfg = _s3_config()
    if cfg and _has_boto3():
        client = _s3_client()
        try:
            client.delete_object(Bucket=cfg["bucket"], Key=_cloud_key(pid))
            # также удаляем локу
            for p in _cloud_dir().glob(f"{pid}*"):
                try:
                    p.unlink()
                except Exception:
                    pass
            return True
        except Exception:
            pass
    lp = _local_path(pid)
    ok = False
    for p in _cloud_dir().glob(f"{pid}*"):
        try:
            p.unlink()
            ok = True
        except Exception:
            pass
    if lp.exists():
        try:
            lp.unlink()
            ok = True
        except Exception:
            pass
    return ok
