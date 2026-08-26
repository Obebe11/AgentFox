import json
import shutil
import tempfile
from pathlib import Path

import core.profile_manager as pm
from core.profile_manager import Profile, create_profile

# изолируем PROFILES_ROOT
_tmp_root = Path(tempfile.mkdtemp(prefix="agentfox_test_io_"))
pm.PROFILES_ROOT = _tmp_root
from core.profile_io import (
    export_profile,
    export_profile_bytes,
    import_profile,
    import_profile_bytes,
)


def _make_dummy(pid: str):
    p = create_profile(pid, geo="DE", targets=["example.com"])
    # добавим файлы в user_data
    (p.user_data_dir / "Default").mkdir(parents=True, exist_ok=True)
    (p.user_data_dir / "Default" / "Preferences").write_text('{"dummy":1}', encoding="utf-8")
    (p.user_data_dir / "session.txt").write_text("hello world", encoding="utf-8")
    # создадим cookie_seed если логика его ищет (опционально)
    (p.dir / "cookie_seed.json").write_text('{"seed":"x"}', encoding="utf-8")
    # создадим .lock который НЕ должен попасть в архив
    (p.dir / ".lock").write_text("owner", encoding="utf-8")
    p.save()
    return p


def test_export_import_roundtrip():
    pid = "roundtrip_001"
    p = _make_dummy(pid)
    orig_meta = json.loads((p.dir / "meta.json").read_text(encoding="utf-8"))
    dest = export_profile(pid)
    assert dest.exists(), "archive not created"
    # архив должен быть .tar.zst т.к. zstandard установлен
    assert dest.suffixes[-2:] == [".tar", ".zst"] or dest.suffix == ".zst"
    # проверим что архив не содержит .lock
    import tarfile

    # простой check через чтение
    tmp_check = Path(tempfile.mktemp(suffix=".tar.zst"))
    # используем import для проверки — удаляем оригинал
    shutil.rmtree(p.dir)
    assert not p.dir.exists()
    prof2 = import_profile(dest, overwrite=False)
    assert prof2.id == pid
    # файлы восстановлены
    assert (prof2.user_data_dir / "session.txt").read_text(encoding="utf-8") == "hello world"
    assert (prof2.user_data_dir / "Default" / "Preferences").exists()
    assert (prof2.dir / "cookie_seed.json").exists()
    assert not (prof2.dir / ".lock").exists(), ".lock should not be restored"
    # meta equality (id, targets, engine)
    loaded_meta = json.loads((prof2.dir / "meta.json").read_text(encoding="utf-8"))
    assert loaded_meta["id"] == orig_meta["id"]
    assert loaded_meta["targets"] == orig_meta["targets"]
    # profile load валиден
    Profile.load(pid)


def test_export_import_with_new_id():
    pid = "orig_002"
    p = _make_dummy(pid)
    dest = export_profile(pid)
    new_id = "cloned_002"
    prof2 = import_profile(dest, new_id=new_id)
    assert prof2.id == new_id
    assert (pm.PROFILES_ROOT / new_id / "meta.json").exists()
    # оригинал остался
    assert (pm.PROFILES_ROOT / pid / "meta.json").exists()
    # user_data скопирован
    assert (pm.PROFILES_ROOT / new_id / "user_data" / "session.txt").read_text(encoding="utf-8") == "hello world"
    # meta.json id патчится
    assert json.loads((pm.PROFILES_ROOT / new_id / "meta.json").read_text())["id"] == new_id


def test_import_overwrite_guard():
    pid = "overwrite_003"
    p = _make_dummy(pid)
    dest = export_profile(pid)
    # импорт без overwrite на существующий id должен упасть
    try:
        import_profile(dest, new_id=pid, overwrite=False)
        assert False, "expected FileExistsError"
    except FileExistsError:
        pass
    # с overwrite=True — ок, содержимое перетирается
    # поменяем файл
    (p.user_data_dir / "session.txt").write_text("modified", encoding="utf-8")
    prof2 = import_profile(dest, new_id=pid, overwrite=True)
    assert (prof2.user_data_dir / "session.txt").read_text(encoding="utf-8") == "hello world"


def test_export_not_found():
    try:
        export_profile("no_such_profile_999")
        assert False, "expected FileNotFoundError"
    except FileNotFoundError:
        pass


def test_tar_gz_fallback():
    # форсируем gzip экспорт через явный dest .tar.gz
    pid = "gztest_004"
    p = _make_dummy(pid)
    dest_gz = pm.PROFILES_ROOT / f"{pid}.tar.gz"
    out = export_profile(pid, dest=dest_gz)
    assert out == dest_gz
    assert out.exists()
    # импорт gz должен работать
    # удаляем профиль и импортируем под новым id
    shutil.rmtree(p.dir)
    prof2 = import_profile(out, new_id="gztest_004_imported")
    assert prof2.id == "gztest_004_imported"
    assert (prof2.user_data_dir / "session.txt").exists()

    # bytes roundtrip
    pid2 = "bytestest_005"
    p2 = _make_dummy(pid2)
    data = export_profile_bytes(pid2)
    assert len(data) > 100
    # magic check — zst или gz
    assert data[:4] == b"\x28\xb5\x2f\xfd" or data[:2] == b"\x1f\x8b"
    prof3 = import_profile_bytes(data, new_id="bytestest_005_clone")
    assert prof3.id == "bytestest_005_clone"
    assert (prof3.user_data_dir / "session.txt").read_text(encoding="utf-8") == "hello world"


def test_atomic_no_tmp_leftover():
    pid = "atomic_006"
    p = _make_dummy(pid)
    dest = export_profile(pid)
    # после экспорта tmp не должен остаться
    assert not (dest.parent / (dest.name + ".tmp")).exists()
    # после импорта tmp_import не должен остаться
    import_profile(dest, new_id="atomic_006_clone")
    assert not (pm.PROFILES_ROOT / "atomic_006_clone.tmp_import").exists()
    assert not (pm.PROFILES_ROOT / "atomic_006.tmp_import").exists()


def test_tar_xz_handling():
    # проверяем что import понимает .tar.xz (создаём вручную из существующего .tar.gz)
    pid = "xztest_007"
    p = _make_dummy(pid)
    dest_gz = export_profile(pid, dest=pm.PROFILES_ROOT / f"{pid}.tar.gz")
    # пережмём в xz через python tarfile для теста fallback
    import tarfile

    xz_path = pm.PROFILES_ROOT / f"{pid}.tar.xz"
    with tarfile.open(str(dest_gz), "r:*") as src:
        with tarfile.open(str(xz_path), "w:xz") as dst:
            for m in src.getmembers():
                f = src.extractfile(m) if m.isreg() else None
                dst.addfile(m, f)
    # импорт xz
    prof2 = import_profile(xz_path, new_id="xztest_007_clone")
    assert prof2.id == "xztest_007_clone"
    assert (prof2.user_data_dir / "session.txt").exists()
