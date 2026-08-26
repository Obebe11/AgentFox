"""Юнит-тесты 2.5: парсинг bot.sannysoft + локальные чекеры + offline прогон через FakePage."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import core.profile_manager as pm

pm.PROFILES_ROOT = Path(tempfile.mkdtemp(prefix="agentfox_t25_"))

from tools.fingerprint_check import (
    classify_bot_cell,
    parse_bot_results,
    evaluate_local_result,
    run_local_checks,
    run_bot_checks,
    run_creepjs_checks,
)


def test_classify_bot_cell():
    assert classify_bot_cell("passed", "") == "PASS"
    assert classify_bot_cell("failed result", "present (failed)") == "FAIL"
    assert classify_bot_cell("warn", "") == "WARN"
    assert classify_bot_cell("", "missing (passed)") == "PASS"
    assert classify_bot_cell("", "") == "SKIPPED"


def test_parse_bot_results_mixed():
    cells = [
        {"id": "webdriver-result", "cls": "passed", "text": "missing (passed)"},
        {"id": "chrome-result", "cls": "failed result", "text": "missing (failed)"},
        {"id": "webgl-vendor", "cls": "", "text": "Intel Inc."},
    ]
    parsed = parse_bot_results(cells)
    assert parsed["webdriver-result"]["status"] == "PASS"
    assert parsed["chrome-result"]["status"] == "FAIL"
    assert parsed["webgl-vendor"]["status"] == "SKIPPED"


def test_evaluate_local_critical_paths():
    assert evaluate_local_result("webdriver", False)["status"] == "PASS"
    assert evaluate_local_result("webdriver", True)["status"] == "FAIL"
    assert evaluate_local_result("plugins_length", 0)["status"] == "FAIL"
    assert evaluate_local_result("plugins_length", 3)["status"] == "PASS"
    assert evaluate_local_result("webgl_vendor", "Brian Paul")["status"] == "FAIL"
    assert evaluate_local_result("webgl_vendor", "Intel Inc.")["status"] == "PASS"
    assert evaluate_local_result("webgl_renderer", "Mesa OffScreen")["status"] == "FAIL"
    assert evaluate_local_result("webgl_renderer", "Intel Iris OpenGL Engine")["status"] == "PASS"
    assert evaluate_local_result("ua_headless", "Mozilla/5.0 HeadlessChrome/120")["status"] == "FAIL"
    assert evaluate_local_result("ua_headless", "Mozilla/5.0 Firefox/128.0")["status"] == "PASS"
    assert evaluate_local_result("chrome", False)["status"] == "INFO"
    assert evaluate_local_result("permissions", "denied+prompt (failed)")["status"] == "FAIL"


# ---- FakePage для run_local/bot/creepjs ----

class FakePage:
    def __init__(self, bot_cells=None, creep_body="creepjs fingerprint trust 100 lies: 0"):
        self._bot_cells = bot_cells
        self._creep_body = creep_body
        self.navigated: list[str] = []

    def goto(self, url, wait_until="domcontentloaded", timeout=30000):
        self.navigated.append(url)

    def content(self):
        return "<html>fake</html>"

    def evaluate(self, js: str):
        # bot cells fetch
        if "user-agent-result" in js:
            if self._bot_cells is not None:
                return self._bot_cells
            return [
                {"id": "webdriver-result", "cls": "passed", "text": "missing (passed)"},
                {"id": "advanced-webdriver-result", "cls": "passed", "text": "passed"},
                {"id": "chrome-result", "cls": "failed result", "text": "missing (failed)"},
                {"id": "permissions-result", "cls": "passed", "text": "granted"},
                {"id": "plugins-length-result", "cls": "passed", "text": "3"},
                {"id": "plugins-type-result", "cls": "passed", "text": "passed"},
                {"id": "languages-result", "cls": "passed", "text": "de-DE,de"},
                {"id": "webgl-vendor", "cls": "passed", "text": "Intel Inc."},
                {"id": "webgl-renderer", "cls": "passed", "text": "Intel Iris OpenGL Engine"},
                {"id": "broken-image-dimensions", "cls": "passed", "text": "16x16"},
                {"id": "user-agent-result", "cls": "passed", "text": "Mozilla/5.0 Firefox/128.0"},
            ]
        if "creepjs" in js.lower() or "document.body.innerText" in js and "fp" in js:
            # creepjs info — called from run_creepjs_checks
            return {"body": self._creep_body, "title": "CreepJS", "fp": "abc"}
        if "document.body.innerText" in js:
            return {"body": self._creep_body, "title": "CreepJS", "fp": ""}
        # local checks — dispatch by substring
        if "navigator.webdriver" in js and "runBotDetection" not in js:
            return False
        if "navigator.userAgent" in js:
            return "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0"
        if "navigator.plugins.length" in js:
            return 3
        if "navigator.languages" in js:
            return "de-DE,de,en-US,en"
        if "window.chrome" in js:
            return False
        if "UNMASKED_VENDOR_WEBGL" in js:
            return "Intel Inc."
        if "UNMASKED_RENDERER_WEBGL" in js:
            return "Intel Iris OpenGL Engine"
        if "_phantom" in js or "__webdriver" in js:
            return False
        if "PluginArray" in js:
            return True
        if "permissions" in js:
            return "granted / Notification.granted"
        if "img" in js and "onerror" in js:
            return "16x16"
        return None


def test_run_local_checks_all_pass_on_clean_fake():
    page = FakePage()
    res = run_local_checks(page)
    # критичные должны быть PASS
    for k in ["webdriver", "webdriver_advanced", "plugins_length", "plugins_type", "languages", "webgl_vendor", "webgl_renderer"]:
        assert res[k]["status"] == "PASS", f"{k} should PASS, got {res[k]}"
    assert res["chrome"]["status"] == "INFO"
    assert res["ua_headless"]["status"] == "PASS"


def test_run_bot_checks_passes_and_chrome_becomes_info():
    page = FakePage()
    rep = run_bot_checks(page)
    assert rep["status"] == "PASS", rep
    assert rep["cells"]["chrome-result"]["status"] == "INFO"
    assert rep["cells"]["webdriver-result"]["status"] == "PASS"


def test_run_bot_checks_detects_fail():
    cells = [
        {"id": "webdriver-result", "cls": "failed", "text": "present (failed)"},
        {"id": "advanced-webdriver-result", "cls": "failed", "text": "failed"},
        {"id": "chrome-result", "cls": "failed result", "text": "missing (failed)"},
        {"id": "plugins-length-result", "cls": "passed", "text": "3"},
        {"id": "plugins-type-result", "cls": "passed", "text": "passed"},
        {"id": "languages-result", "cls": "passed", "text": "de-DE"},
        {"id": "webgl-vendor", "cls": "passed", "text": "Intel Inc."},
        {"id": "webgl-renderer", "cls": "passed", "text": "Intel Iris"},
        {"id": "broken-image-dimensions", "cls": "passed", "text": "16x16"},
        {"id": "user-agent-result", "cls": "passed", "text": "Mozilla/5.0"},
        {"id": "permissions-result", "cls": "passed", "text": "granted"},
    ]
    page = FakePage(bot_cells=cells)
    rep = run_bot_checks(page)
    assert rep["status"] == "FAIL"
    assert rep["cells"]["webdriver-result"]["status"] == "FAIL"


def test_run_creepjs_pass_and_skipped():
    page = FakePage(creep_body="CreepJS Fingerprint trust score 100 — clean " + "x" * 600)
    rep = run_creepjs_checks(page)
    assert rep["status"] == "PASS"
    page2 = FakePage(creep_body="short")
    rep2 = run_creepjs_checks(page2)
    assert rep2["status"] == "SKIPPED"


def test_collect_one_offline_with_fake_engine(monkeypatch=None):
    # интеграция collect_one в offline режиме через FakeEngine
    import tools.fingerprint_check as fc
    import api.server  # noqa: ensure import

    class FakeMouse:
        def wheel(self, *a, **k): pass
        def move(self, *a, **k): pass
        def click(self, *a, **k): pass

    class FakeEngine:
        def __init__(self):
            self.page = FakePage()
            self.page.mouse = FakeMouse()

        def launch(self, profile, headless=True):
            return self.page

        def close(self):
            pass

    fake = FakeEngine()
    # monkeypatch get_engine
    import core.session as sess

    orig = sess.get_engine
    sess.get_engine = lambda p: fake
    # also patch tools module's import path (collect_one does `from core.session import get_engine` inside)
    # so patch sess.get_engine is enough (same object)
    try:
        rep = fc.collect_one("fpcheck_unit_1", geo="DE", headless=True, live=False, timeout=10000)
        assert rep["overall"] == "PASS", rep
        assert rep["local_overall"] == "PASS"
        assert rep["bot"]["status"] == "SKIPPED"
        assert "os" in rep and "locale" in rep
    finally:
        sess.get_engine = orig
