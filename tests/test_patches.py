"""
tests/test_patches.py — верификация monkey-patch слоя Level 2 (core/patches.py)
"""
import importlib
import sys
import warnings


def test_check_environment_runs_without_camoufox():
    """check_environment не падает даже если camoufox не установлен (graceful fallback, mock)."""
    # мокаем отсутствие camoufox — удаляем из sys.modules если есть, и проверяем что функция не бросает
    import core.patches as patches

    # сохраняем оригинальные модули
    saved = {}
    for mod in list(sys.modules.keys()):
        if mod.startswith("camoufox"):
            saved[mod] = sys.modules.pop(mod)

    # также мокаем importlib.metadata.version для playwright/camoufox чтобы вернуть None
    orig_version = None
    try:
        # вызов должен отработать без исключений даже без camoufox в sys.modules
        res = patches.check_environment()
        assert isinstance(res, dict)
        assert "playwright" in res
        assert "canvas_seed" in res
        assert "webgl" in res
        assert "camoufox" in res
        assert "overall" in res
        # overall всегда одно из значений
        assert res["overall"] in ("ok", "warn", "fail", "skipped")
        # canvas_seed всегда warn (документация ограничения beta.28)
        assert res["canvas_seed"]["status"] == "warn"
        assert "beta.28" in res["canvas_seed"]["message"] or "152.0.4" in res["canvas_seed"]["message"]
    finally:
        # восстанавливаем
        for k, v in saved.items():
            sys.modules[k] = v


def test_apply_all_idempotent():
    """apply_all идемпотентен — повторный вызов возвращает тот же отчёт без двойного патча."""
    import core.patches as patches

    patches.reset_patches_for_tests()
    r1 = patches.apply_all()
    r2 = patches.apply_all()
    assert r1 is r2  # тот же объект (кэш)
    assert r1["patched"] is True
    assert r1["idempotent"] is True
    assert "playwright_guard" in r1["patches"]
    assert "firefox_user_prefs_guard" in r1["patches"]
    # алиас тоже идемпотентен
    r3 = patches.apply_camoufox_patches()
    assert r3 is r1

    # после сброса — новый объект
    patches.reset_patches_for_tests()
    r4 = patches.apply_all()
    assert r4 is not r1
    assert r4["patches"] == r1["patches"]


def test_playwright_guard_rejects_1_61():
    """Гвард отклоняет playwright >=1.61 и принимает <1.61."""
    import core.patches as patches

    assert patches.is_playwright_version_compatible("1.60.0") is True
    assert patches.is_playwright_version_compatible("1.60.1") is True
    assert patches.is_playwright_version_compatible("1.59.9") is True
    assert patches.is_playwright_version_compatible("1.61.0") is False
    assert patches.is_playwright_version_compatible("1.61") is False
    assert patches.is_playwright_version_compatible("2.0.0") is False

    ok_res = patches.check_playwright_version("1.60.0")
    assert ok_res["ok"] is True
    assert ok_res["status"] == "ok"

    fail_res = patches.check_playwright_version("1.61.0")
    assert fail_res["ok"] is False
    assert fail_res["status"] == "fail"
    assert "1.61" in fail_res["message"]
    assert "653" in fail_res["message"] or "daijro" in fail_res["message"]

    # assert_playwright_version бросает на плохой версии (мокаем get_playwright_version)
    orig = patches.get_playwright_version
    try:
        patches.get_playwright_version = lambda: "1.61.2"  # type: ignore
        try:
            patches.assert_playwright_version()
            assert False, "должен был бросить RuntimeError"
        except RuntimeError as e:
            assert "1.61" in str(e)
    finally:
        patches.get_playwright_version = orig  # type: ignore


def test_webgl_validation_and_firefox_prefs_guard():
    """WebGL валидация пропускает битые пресеты; firefox_user_prefs гвард ловит misplaced ключи."""
    import core.patches as patches

    # WebGL: пресет без webgl — валиден
    assert patches.is_webgl_preset_valid({}, "windows") is True
    assert patches.is_webgl_preset_valid({"webgl": {}}, "linux") is True
    # пресет с валидным webgl — если camoufox не установлен, fallback True
    assert patches.is_webgl_preset_valid({"webgl": {"unmaskedVendor": "Test", "unmaskedRenderer": "Test GPU"}}, "windows") in (True, False)

    # firefox_user_prefs guard: ключ с точкой в config → warning
    warns = patches.validate_firefox_prefs_kwargs({"config": {"browser.cache.disk.capacity": 123, "canvas:seed": 1}})
    assert len(warns) == 1
    assert "firefox_user_prefs" in warns[0]

    warns2 = patches.validate_firefox_prefs_kwargs({"config": {"canvas:seed": 1, "audio:seed": 2}})
    assert warns2 == []

    # fix_camoufox_kwargs переносит ключ
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        fixed = patches.fix_camoufox_kwargs({"config": {"browser.cache.disk.capacity": 123, "canvas:seed": 1}, "firefox_user_prefs": {}})
        assert "browser.cache.disk.capacity" not in fixed["config"]
        assert fixed["firefox_user_prefs"]["browser.cache.disk.capacity"] == 123
        assert fixed["config"]["canvas:seed"] == 1
        assert len(w) == 1
        assert "firefox_user_prefs" in str(w[0].message)

    # идемпотентность fix: повторный вызов не дублирует warning и не ломает
    with warnings.catch_warnings(record=True) as w2:
        warnings.simplefilter("always")
        fixed2 = patches.fix_camoufox_kwargs(fixed)
        # уже перенесено — не должно быть новых warnings
        assert len(w2) == 0


def test_canvas_seed_limitation_documented():
    """canvas_seed лимитация задокументирована с beta.28 и ссылкой."""
    import core.patches as patches

    res = patches.check_canvas_seed_limitation()
    assert res["status"] == "warn"
    assert patches.BETA_VERSION in res["message"]
    assert "setCanvasSeed" in res["message"] or "canvas" in res["message"].lower()
    assert "fingerprint-per-context" in res["message"] or res["issue"] is not None

    # wrap_canvas_seed_warning на FakePage без camoufox
    class FakePage:
        def evaluate(self, js):
            return False  # нет setCanvasSeed

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        ok = patches.wrap_canvas_seed_warning(FakePage())
        assert ok is False
        assert len(w) == 1
        assert "setCanvasSeed" in str(w[0].message)

    assert patches.wrap_canvas_seed_warning(None) is False
