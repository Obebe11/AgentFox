"""
profile_io — экспорт / импорт профилей (tar.zst + fallback).

Архив содержит:
  - meta.json
  - user_data/ (рекурсивно, без .lock / *.tmp)
  - cookie_seed.json (если есть)
  - .agentfox_manifest.json

Поддерживаемые расширения: .tar.zst / .tar.gz / .tgz / .tar.xz / .tar.bz2 / .tar
Предпочтение — zstandard (.tar.zst), fallback — gzip (.tar.gz) / lzma (.tar.xz via stdlib).
"""
from __future__ import annotations

import io
import json
import shutil
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .profile_manager import Profile
import core.profile_manager as _pm

def _profiles_root() -> Path:
    return _pm.PROFILES_ROOT

__all__ = [
    "export_profile",
    "import_profile",
    "export_profile_bytes",
    "import_profile_bytes",
]

_MANIFEST_NAME = ".agentfox_manifest.json"
_VERSION = 1


def _has_zstd() -> bool:
    try:
        import zstandard  # noqa: F401

        return True
    except ImportError:
        return False


def _detect_compression(name: str) -> str:
    n = name.lower()
    if n.endswith(".tar.zst") or n.endswith(".tzst") or n.endswith(".zst"):
        return "zst"
    if n.endswith(".tar.gz") or n.endswith(".tgz"):
        return "gz"
    if n.endswith(".tar.xz") or n.endswith(".txz"):
        return "xz"
    if n.endswith(".tar.bz2") or n.endswith(".tbz2") or n.endswith(".tbz"):
        return "bz2"
    if n.endswith(".tar"):
        return ""
    return ""


def _choose_export_compression() -> str:
    if _has_zstd():
        return "zst"
    return "gz"


def _default_dest(pid: str, compression: str) -> Path:
    ext_map = {"zst": ".tar.zst", "gz": ".tar.gz", "xz": ".tar.xz", "bz2": ".tar.bz2", "": ".tar"}
    ext = ext_map.get(compression, ".tar.gz")
    return _profiles_root() / f"{pid}{ext}"


def _should_skip(p: Path) -> bool:
    # .lock и *.tmp не попадают в архив
    if p.name == ".lock":
        return True
    if p.suffix == ".tmp" or p.name.endswith(".tmp"):
        return True
    return False


def _is_safe_member(name: str) -> bool:
    if not name or name in (".", "./"):
        return False
    # абсолютные пути запрещены
    if name.startswith("/") or name.startswith("\\"):
        return False
    # path traversal
    parts = Path(name).parts
    if ".." in parts:
        return False
    # .lock / *.tmp не извлекаем (даже если вдруг в архиве)
    base = Path(name).name
    if base == ".lock":
        return False
    if base.endswith(".tmp"):
        return False
    return True


def _add_manifest(tar: tarfile.TarFile, pid: str) -> None:
    manifest = {
        "version": _VERSION,
        "profile_id": pid,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "format": "agentfox-profile-v1",
    }
    data = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    ti = tarfile.TarInfo(name=_MANIFEST_NAME)
    ti.size = len(data)
    ti.mtime = int(datetime.now(timezone.utc).timestamp())
    ti.mode = 0o644
    tar.addfile(ti, io.BytesIO(data))


def _export_to_tar(pid: str, tmp_path: Path, compression: str) -> None:
    profile_dir = _profiles_root() / pid
    meta_path = profile_dir / "meta.json"
    if not meta_path.exists():
        raise FileNotFoundError(f"profile {pid} not found")

    # cookie_seed.json + history.jsonl опционально (рядом с профилем)
    extra_files: list[Path] = []
    for cand in [profile_dir / "cookie_seed.json", profile_dir / "cookies_seed.json", profile_dir / "history.jsonl"]:
        if cand.exists() and not _should_skip(cand):
            extra_files.append(cand)

    user_data = profile_dir / "user_data"

    if compression == "zst":
        import zstandard as zstd

        # streaming write: raw -> zstd -> tar (mode w|)
        with open(tmp_path, "wb") as raw:
            cctx = zstd.ZstdCompressor(level=3)
            with cctx.stream_writer(raw) as zfh:
                with tarfile.open(fileobj=zfh, mode="w|", format=tarfile.PAX_FORMAT) as tar:
                    tar.add(str(meta_path), arcname="meta.json", recursive=False)
                    for ef in extra_files:
                        tar.add(str(ef), arcname=ef.name, recursive=False)
                    _add_manifest(tar, pid)
                    if user_data.exists():
                        # H8: preserve mtime — явно добавляем user_data + все директории до файлов
                        ti = tarfile.TarInfo(name="user_data")
                        ti.type = tarfile.DIRTYPE
                        ti.mtime = int(user_data.stat().st_mtime)
                        ti.mode = user_data.stat().st_mode & 0o777 or 0o755
                        tar.addfile(ti)
                        for item in sorted(user_data.rglob("*"), key=lambda p: (len(p.parts), str(p))):
                            if _should_skip(item):
                                continue
                            arc = str(item.relative_to(profile_dir))
                            if item.is_symlink():
                                tar.add(str(item), arcname=arc, recursive=False)
                            elif item.is_dir():
                                ti = tarfile.TarInfo(name=arc)
                                ti.type = tarfile.DIRTYPE
                                ti.mtime = int(item.stat().st_mtime)
                                ti.mode = item.stat().st_mode & 0o777 or 0o755
                                tar.addfile(ti)
                            else:
                                tar.add(str(item), arcname=arc, recursive=False)
    elif compression == "gz":
        with tarfile.open(str(tmp_path), mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
            tar.add(str(meta_path), arcname="meta.json")
            for ef in extra_files:
                tar.add(str(ef), arcname=ef.name)
            _add_manifest(tar, pid)
            if user_data.exists():
                # tar.add с фильтром чтобы исключить .lock
                def _filter(ti: tarfile.TarInfo) -> tarfile.TarInfo | None:
                    if ti.name.endswith(".lock") or ti.name.endswith(".tmp"):
                        return None
                    return ti

                # H8: preserve mtime — явно добавляем user_data и все директории
                ti = tarfile.TarInfo(name="user_data")
                ti.type = tarfile.DIRTYPE
                ti.mtime = int(user_data.stat().st_mtime)
                ti.mode = user_data.stat().st_mode & 0o777 or 0o755
                flt = _filter(ti)
                if flt is not None:
                    tar.addfile(flt)
                for item in sorted(user_data.rglob("*"), key=lambda p: (len(p.parts), str(p))):
                    if _should_skip(item):
                        continue
                    arc = str(item.relative_to(profile_dir))
                    if item.is_symlink():
                        tar.add(str(item), arcname=arc, recursive=False, filter=lambda ti: _filter(ti))
                    elif item.is_dir():
                        ti = tarfile.TarInfo(name=arc)
                        ti.type = tarfile.DIRTYPE
                        ti.mtime = int(item.stat().st_mtime)
                        ti.mode = item.stat().st_mode & 0o777 or 0o755
                        flt = _filter(ti)
                        if flt is not None:
                            tar.addfile(flt)
                    else:
                        tar.add(str(item), arcname=arc, recursive=False, filter=lambda ti: _filter(ti))
    elif compression == "xz":
        with tarfile.open(str(tmp_path), mode="w:xz", format=tarfile.PAX_FORMAT) as tar:
            tar.add(str(meta_path), arcname="meta.json")
            for ef in extra_files:
                tar.add(str(ef), arcname=ef.name)
            _add_manifest(tar, pid)
            if user_data.exists():
                # H8: preserve mtime
                ti = tarfile.TarInfo(name="user_data")
                ti.type = tarfile.DIRTYPE
                ti.mtime = int(user_data.stat().st_mtime)
                ti.mode = user_data.stat().st_mode & 0o777 or 0o755
                tar.addfile(ti)
                for item in sorted(user_data.rglob("*"), key=lambda p: (len(p.parts), str(p))):
                    if _should_skip(item):
                        continue
                    arc = str(item.relative_to(profile_dir))
                    if item.is_symlink():
                        tar.add(str(item), arcname=arc, recursive=False)
                    elif item.is_dir():
                        ti = tarfile.TarInfo(name=arc)
                        ti.type = tarfile.DIRTYPE
                        ti.mtime = int(item.stat().st_mtime)
                        ti.mode = item.stat().st_mode & 0o777 or 0o755
                        tar.addfile(ti)
                    else:
                        tar.add(str(item), arcname=arc, recursive=False)
    else:
        with tarfile.open(str(tmp_path), mode="w", format=tarfile.PAX_FORMAT) as tar:
            tar.add(str(meta_path), arcname="meta.json")
            for ef in extra_files:
                tar.add(str(ef), arcname=ef.name)
            _add_manifest(tar, pid)
            if user_data.exists():
                # H8: preserve mtime
                ti = tarfile.TarInfo(name="user_data")
                ti.type = tarfile.DIRTYPE
                ti.mtime = int(user_data.stat().st_mtime)
                ti.mode = user_data.stat().st_mode & 0o777 or 0o755
                tar.addfile(ti)
                for item in sorted(user_data.rglob("*"), key=lambda p: (len(p.parts), str(p))):
                    if _should_skip(item):
                        continue
                    arc = str(item.relative_to(profile_dir))
                    if item.is_symlink():
                        tar.add(str(item), arcname=arc, recursive=False)
                    elif item.is_dir():
                        ti = tarfile.TarInfo(name=arc)
                        ti.type = tarfile.DIRTYPE
                        ti.mtime = int(item.stat().st_mtime)
                        ti.mode = item.stat().st_mode & 0o777 or 0o755
                        tar.addfile(ti)
                    else:
                        tar.add(str(item), arcname=arc, recursive=False)


def export_profile(pid: str, dest: Path | None = None) -> Path:
    """Экспорт профиля в tar.zst (или tar.gz fallback). Атомарно. Возвращает путь к архиву."""
    profile_dir = _profiles_root() / pid
    if not (profile_dir / "meta.json").exists():
        raise FileNotFoundError(f"profile {pid} not found")

    if dest is None:
        compression = _choose_export_compression()
        dest = _default_dest(pid, compression)
    else:
        dest = Path(dest)
        # если dest — директория, добавляем имя файла
        if dest.is_dir():
            compression = _choose_export_compression()
            ext = ".tar.zst" if compression == "zst" else ".tar.gz"
            dest = dest / f"{pid}{ext}"
        else:
            compression = _detect_compression(dest.name)
            if not compression:
                # без расширения — считаем gz fallback или plain
                compression = _choose_export_compression()
                # не меняем dest, пишем как есть с выбранной компрессией
                if compression == "zst" and not dest.name.endswith(".zst"):
                    pass  # пишем zst даже если расширение не .zst — ок
            # если пользователь явно указал .tar.zst но zstd нет — fallback к gz с предупреждением в имени?
            if compression == "zst" and not _has_zstd():
                # fallback к gz, но сохраняем запрошенное имя? лучше поменять расширение на .tar.gz
                # чтобы архив был валидным gzip
                if dest.name.endswith(".tar.zst") or dest.name.endswith(".zst"):
                    dest = dest.with_name(dest.name.replace(".tar.zst", ".tar.gz").replace(".zst", ".gz"))
                    if dest.name.endswith(".gz.gz"):
                        dest = Path(str(dest).replace(".gz.gz", ".gz"))
                compression = "gz"

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / (dest.name + ".tmp")
    # cleanup stale tmp
    tmp.unlink(missing_ok=True)
    try:
        _export_to_tar(pid, tmp, compression)
        tmp.replace(dest)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return dest


# ---- import helpers ----

def _open_tar_for_read(archive: Path):
    """Возвращает контекстный менеджер TarFile. Для zst — streaming."""
    compression = _detect_compression(archive.name)
    # также пробуем magic для файлов без расширения
    if not compression:
        try:
            with open(archive, "rb") as f:
                magic = f.read(4)
                if magic == b"\x28\xb5\x2f\xfd":
                    compression = "zst"
                elif magic[:2] == b"\x1f\x8b":
                    compression = "gz"
                elif magic[:3] == b"\xfd7z":
                    compression = "xz"
        except Exception:
            pass

    if compression == "zst":
        if not _has_zstd():
            raise RuntimeError("zstandard not installed but archive is .tar.zst — pip install zstandard")
        import zstandard as zstd

        raw = open(archive, "rb")
        dctx = zstd.ZstdDecompressor()
        stream = dctx.stream_reader(raw)
        # tar streaming
        tar = tarfile.open(fileobj=stream, mode="r|*")
        # возвращаем тройку для закрытия
        class _Ctx:
            def __enter__(self):
                return tar

            def __exit__(self, *a):
                try:
                    tar.close()
                finally:
                    try:
                        stream.close()
                    except Exception:
                        pass
                    raw.close()

        return _Ctx()
    else:
        # tarfile авто-детект gz/xz/bz2/plain
        return tarfile.open(str(archive), mode="r:*")


def _extract_tar(archive: Path, tmp_dir: Path) -> None:
    tmp_dir.mkdir(parents=True, exist_ok=True)
    # для обычного tar (не zst) используем r:* и extractall с фильтрацией
    compression = _detect_compression(archive.name)
    # magic fallback
    if not compression:
        try:
            with open(archive, "rb") as f:
                magic = f.read(4)
                if magic == b"\x28\xb5\x2f\xfd":
                    compression = "zst"
        except Exception:
            pass

    if compression == "zst":
        # streaming extraction
        dirs_to_restore: list[tuple[Path, int]] = []
        with _open_tar_for_read(archive) as tar:
            for member in tar:
                name = member.name.lstrip("./")
                if not _is_safe_member(name):
                    continue
                # normalize
                # member.name may contain ./ prefix
                target = tmp_dir / name
                # path traversal double-check
                try:
                    target.resolve().relative_to(tmp_dir.resolve())
                except Exception:
                    # fallback simple check
                    if ".." in Path(name).parts:
                        continue
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    try:
                        target.chmod(member.mode & 0o777)
                    except Exception:
                        pass
                    dirs_to_restore.append((target, member.mtime))
                elif member.issym() or member.islnk():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        # linkname may be absolute — skip unsafe
                        if member.linkname.startswith("/") or ".." in Path(member.linkname).parts:
                            continue
                        if target.exists() or target.is_symlink():
                            target.unlink()
                        target.symlink_to(member.linkname)
                    except Exception:
                        pass
                elif member.isreg():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    f = tar.extractfile(member)
                    if f is None:
                        continue
                    with open(target, "wb") as out:
                        shutil.copyfileobj(f, out)
                    try:
                        target.chmod(member.mode & 0o777)
                    except Exception:
                        pass
                    # preserve mtime
                    try:
                        import os

                        os.utime(target, (member.mtime, member.mtime))
                    except Exception:
                        pass
                else:
                    # other types (fifo, etc) skip
                    continue
        # H8: restore dir mtimes after files (files touch parent mtime)
        for dpath, mtime in dirs_to_restore:
            try:
                import os

                os.utime(dpath, (mtime, mtime))
            except Exception:
                pass
    else:
        dirs_to_restore: list[tuple[Path, int]] = []
        with _open_tar_for_read(archive) as tar:
            for member in tar.getmembers():
                name = member.name.lstrip("./")
                if not _is_safe_member(name):
                    continue
                # sanitize name for extraction
                member.name = name
                # ensure path inside tmp_dir
                target = tmp_dir / name
                # traversal check
                try:
                    target.resolve().relative_to(tmp_dir.resolve())
                except Exception:
                    if ".." in Path(name).parts:
                        continue
                # extract safely
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    try:
                        target.chmod(member.mode & 0o777)
                    except Exception:
                        pass
                    dirs_to_restore.append((target, member.mtime))
                elif member.issym() or member.islnk():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if member.linkname.startswith("/") or ".." in Path(member.linkname).parts:
                        continue
                    try:
                        if target.exists() or target.is_symlink():
                            target.unlink()
                        target.symlink_to(member.linkname)
                    except Exception:
                        pass
                elif member.isreg():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    f = tar.extractfile(member)
                    if f is None:
                        continue
                    with open(target, "wb") as out:
                        shutil.copyfileobj(f, out)
                    try:
                        target.chmod(member.mode & 0o777)
                    except Exception:
                        pass
                    try:
                        import os

                        os.utime(target, (member.mtime, member.mtime))
                    except Exception:
                        pass


def import_profile(archive: Path, new_id: str | None = None, overwrite: bool = False) -> Profile:
    """Импорт профиля из архива. Возвращает загруженный Profile."""
    archive = Path(archive)
    if not archive.exists():
        raise FileNotFoundError(f"archive {archive} not found")

    # определяем исходный id из архива (meta.json)
    original_id: str | None = None
    try:
        compression = _detect_compression(archive.name)
        if not compression:
            try:
                with open(archive, "rb") as _f:
                    _magic = _f.read(4)
                    if _magic == b"\x28\xb5\x2f\xfd":
                        compression = "zst"
            except Exception:
                pass
        if compression == "zst":
            # streaming — читаем последовательно, extractfile сразу
            with _open_tar_for_read(archive) as tar:
                for m in tar:
                    n = m.name.lstrip("./")
                    if n == "meta.json":
                        try:
                            f = tar.extractfile(m)
                            if f is not None:
                                data = json.loads(f.read().decode("utf-8"))
                                original_id = data.get("id")
                        except Exception:
                            pass
                        break
        else:
            with tarfile.open(str(archive), mode="r:*") as tar:
                try:
                    m = tar.getmember("meta.json")
                except KeyError:
                    # может быть ./meta.json
                    m = None
                    for cand in tar.getmembers():
                        if cand.name.lstrip("./") == "meta.json":
                            m = cand
                            break
                if m is not None:
                    f = tar.extractfile(m)
                    if f is not None:
                        data = json.loads(f.read().decode("utf-8"))
                        original_id = data.get("id")
    except RuntimeError:
        raise
    except Exception:
        pass

    pid = new_id or original_id
    if not pid:
        raise ValueError("cannot determine profile id from archive and new_id not provided")

    # валидация id — простые ограничения как в filesystem
    pid = pid.strip()
    if not pid or "/" in pid or "\\" in pid or ".." in pid:
        raise ValueError(f"invalid profile id: {pid!r}")

    dest_dir = _profiles_root() / pid
    if dest_dir.exists() and (dest_dir / "meta.json").exists() and not overwrite:
        raise FileExistsError(f"profile {pid} already exists (use overwrite=True)")

    tmp_dir = dest_dir.parent / (dest_dir.name + ".tmp_import")
    # cleanup stale tmp
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir, ignore_errors=True)

    try:
        _extract_tar(archive, tmp_dir)

        # валидация что meta.json есть
        meta = tmp_dir / "meta.json"
        if not meta.exists():
            raise ValueError("archive missing meta.json — not a valid AgentFox profile export")

        # проверяем что meta.json парсится и содержит id
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except Exception as e:
            raise ValueError(f"invalid meta.json in archive: {e}") from e

        # если new_id задан и отличается от id в meta.json — патчим meta.json
        if new_id and data.get("id") != new_id:
            data["id"] = new_id
            # также попеправить identity id если есть
            if isinstance(data.get("identity"), dict):
                data["identity"]["id"] = new_id
            tmp_dir.joinpath("meta.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

        # атомарная установка
        if dest_dir.exists():
            shutil.rmtree(dest_dir, ignore_errors=True)
        # ensure parent exists
        dest_dir.parent.mkdir(parents=True, exist_ok=True)
        tmp_dir.replace(dest_dir)

        # валидация через Profile.load
        prof = Profile.load(pid)
        return prof
    except Exception:
        # cleanup tmp
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
        raise


def export_profile_bytes(pid: str) -> bytes:
    """Экспорт в bytes (для API)."""
    # создаём временный файл и читаем
    comp = _choose_export_compression()
    suffix = ".tar.zst" if comp == "zst" else ".tar.gz"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tf:
        tmp = Path(tf.name)
    tmp.unlink(missing_ok=True)
    try:
        # напрямую вызываем внутренний экспорт чтобы не зависеть от атомарного dest
        _export_to_tar(pid, tmp, comp)
        return tmp.read_bytes()
    finally:
        tmp.unlink(missing_ok=True)


def import_profile_bytes(data: bytes, new_id: str | None = None, overwrite: bool = False) -> Profile:
    """Импорт из bytes."""
    # определяем расширение по magic
    suffix = ".tar.gz"
    if data[:4] == b"\x28\xb5\x2f\xfd":
        suffix = ".tar.zst"
    elif data[:2] == b"\x1f\x8b":
        suffix = ".tar.gz"
    elif data[:3] == b"\xfd7z":
        suffix = ".tar.xz"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tf:
        tf.write(data)
        tmp = Path(tf.name)
    try:
        return import_profile(tmp, new_id=new_id, overwrite=overwrite)
    finally:
        tmp.unlink(missing_ok=True)


# ---- CLI ----
if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="AgentFox profile export/import")
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("export", help="export profile")
    e.add_argument("pid", help="profile id")
    e.add_argument("dest", nargs="?", default=None, help="destination file or dir")

    i = sub.add_parser("import", help="import profile")
    i.add_argument("archive", help="archive path")
    i.add_argument("--new-id", dest="new_id", default=None, help="new profile id")
    i.add_argument("--overwrite", action="store_true", help="overwrite if exists")

    args = p.parse_args()
    if args.cmd == "export":
        out = export_profile(args.pid, Path(args.dest) if args.dest else None)
        print(str(out))
    elif args.cmd == "import":
        prof = import_profile(Path(args.archive), new_id=args.new_id, overwrite=args.overwrite)
        print(f"imported {prof.id} -> {prof.dir}")
