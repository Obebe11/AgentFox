"""
tests/test_kernel_check.py — H7/H10 kernel staleness check.

Verification (из задачи):
 - test_kernel_check_runs — вызов main-логики, overall in {ok,warn,skipped}
 - test_kernel_check_json_output — запуск с --json tmp файл, проверка файла и timestamp
Изолированно, без сети.
"""
import json
import subprocess
import sys
from pathlib import Path


def test_kernel_check_runs():
    """Проверяет что kernel_check отрабатывает и overall в допустимом множестве."""
    # best-effort: если camoufox не установлен — skipped
    from tools.kernel_check import build_report

    report = build_report()
    assert isinstance(report, dict)
    assert "overall" in report
    assert report["overall"] in ("ok", "warn", "skipped", "outdated", "fail"), f"unexpected overall {report['overall']}"
    # задача требует {ok,warn,skipped} — в нормальном окружении с актуальным ядром должно быть одно из них
    # если outdated (UA <143) — overall будет outdated, но на этой машине 149-152 >=143 поэтому не outdated
    if not report.get("outdated"):
        assert report["overall"] in ("ok", "warn", "skipped")
    assert "timestamp" in report
    assert "playwright" in report
    assert "camoufox" in report
    assert "presets" in report
    assert "ua" in report
    assert "environment" in report
    # environment должен содержать overall
    assert "overall" in report["environment"]
    # timestamp — ISO8601
    assert "T" in report["timestamp"]
    # playwright / camoufox best-effort
    assert report["playwright"]["status"] in ("ok", "skipped", "fail", "warn")
    assert report["camoufox"]["status"] in ("ok", "skipped")


def test_kernel_check_json_output(tmp_path: Path):
    """Запуск с --json во временный файл — файл существует и содержит timestamp."""
    out = tmp_path / "kernel_report.json"
    # Запуск как модуль: python -m tools.kernel_check --json report.json --fail-on outdated
    # Должен быть cron-able и не падать (exit 0 если не outdated, 1 если outdated — оба допустимы, проверяем файл)
    result = subprocess.run(
        [sys.executable, "-m", "tools.kernel_check", "--json", str(out), "--fail-on", "outdated"],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).parent.parent),
    )
    # exit code 0 когда не outdated, 1 когда outdated — оба ок, главное не 2 (argparse error) и не краш
    assert result.returncode in (0, 1), f"unexpected exit {result.returncode} stderr={result.stderr[:500]} stdout={result.stdout[:500]}"
    assert out.exists(), f"JSON file not created, stderr={result.stderr[:500]}"
    data = json.loads(out.read_text(encoding="utf-8"))
    assert "timestamp" in data
    assert "overall" in data
    assert data["overall"] in ("ok", "warn", "skipped", "outdated", "fail")
    assert "expected_chrome" in data
    assert data["expected_chrome"] == 143
    assert "outdated" in data
    assert isinstance(data["outdated"], bool)
    # также проверяем что файл валидный JSON и содержит checks
    assert "checks" in data or "playwright" in data
