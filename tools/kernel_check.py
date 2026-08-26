#!/usr/bin/env python3
"""
tools.kernel_check — H7/H10 kernel staleness check (cron-able).

Проверяет:
 - core.patches.check_environment() (playwright guard, canvas_seed, webgl, camoufox)
 - playwright версия (совместимость <1.61)
 - camoufox версия (cloverlabs-camoufox)
 - browser kernel (BETA_VERSION 152.0.4-beta.28) vs fingerprint-presets-v150.json
   (min_firefox_version, presets counts, sample UA)
 - UA (rv:XX из preset) vs ожидаемый Chrome 143 (янв 2026) — флаг отставания

Cron:
  python -m tools.kernel_check --json report.json --fail-on outdated
  # exit 1 если kernel outdated (UA < 143), иначе 0; skipped если camoufox не установлен.

Best-effort: если camoufox/playwright не установлены — проверки помечаются skipped, не падают.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

EXPECTED_CHROME = 143
EXPECTED_CHROME_MAJOR = 143

PRESETS_FILENAME = "fingerprint-presets-v150.json"

def _parse_major(v: str) -> Optional[int]:
    try:
        m = re.search(r"(\d+)", v.strip())
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return None

def _extract_ua_major(ua: str) -> Optional[int]:
    if not ua:
        return None
    m = re.search(r"rv:(\d+)", ua)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return None
    # fallback Chrome/XX
    m2 = re.search(r"Chrome/(\d+)", ua)
    if m2:
        try:
            return int(m2.group(1))
        except Exception:
            return None
    return None

def get_playwright_info() -> dict[str, Any]:
    try:
        from core.patches import check_playwright_version, get_playwright_version

        ver = get_playwright_version()
        res = check_playwright_version()
        # res already has ok, status, installed_version, required, message
        return {
            "version": ver,
            "installed_version": res.get("installed_version"),
            "required": res.get("required", "<1.61"),
            "ok": res.get("ok", True),
            "status": res.get("status", "ok" if res.get("ok") else "fail"),
            "message": res.get("message", ""),
        }
    except Exception as e:
        # best-effort fallback via importlib.metadata
        try:
            from importlib.metadata import version as pkg_version

            ver = pkg_version("playwright")
            return {"version": ver, "installed_version": ver, "required": "<1.61", "ok": True, "status": "ok", "message": f"playwright {ver} via metadata"}
        except Exception:
            return {"version": None, "installed_version": None, "required": "<1.61", "ok": True, "status": "skipped", "message": f"playwright not installed, skipped: {e}"[:300]}

def get_camoufox_info() -> dict[str, Any]:
    try:
        from importlib.metadata import version as pkg_version

        v = pkg_version("cloverlabs-camoufox")
        return {"version": v, "ok": True, "status": "ok", "message": f"camoufox {v}"}
    except Exception as e1:
        try:
            import camoufox  # noqa: F401

            return {"version": "unknown (importable)", "ok": True, "status": "ok", "message": "camoufox importable but version unknown"}
        except Exception as e2:
            return {"version": None, "ok": True, "status": "skipped", "message": f"camoufox not installed — check skipped: {e2}"[:300]}

def get_kernel_info() -> dict[str, Any]:
    try:
        from core.patches import BETA_VERSION

        major = _parse_major(BETA_VERSION)
        return {"beta_version": BETA_VERSION, "major": major, "status": "ok", "ok": True}
    except Exception as e:
        return {"beta_version": None, "major": None, "status": "skipped", "ok": True, "message": str(e)[:200]}

def get_presets_info() -> dict[str, Any]:
    try:
        import importlib.resources as res
        import json as _json

        text = res.files("camoufox").joinpath(PRESETS_FILENAME).read_text(encoding="utf-8")
        data = _json.loads(text)
        presets = data.get("presets", {})
        counts = {k: len(v) for k, v in presets.items()} if isinstance(presets, dict) else {}
        # sample UA: first available preset
        sample_ua: Optional[str] = None
        ua_major: Optional[int] = None
        all_majors: list[int] = []
        # collect all UA majors
        if isinstance(presets, dict):
            for lst in presets.values():
                if not isinstance(lst, list):
                    continue
                for p in lst:
                    try:
                        ua = (p.get("navigator") or {}).get("userAgent") or ""
                        maj = _extract_ua_major(ua)
                        if maj is not None:
                            all_majors.append(maj)
                    except Exception:
                        continue
            # sample per os preference windows -> macos -> linux
            for osk in ["windows", "macos", "linux"]:
                lst = presets.get(osk) or []
                if lst:
                    first = lst[0]
                    sample_ua = (first.get("navigator") or {}).get("userAgent") or ""
                    ua_major = _extract_ua_major(sample_ua)
                    break
        uniq_majors = sorted(set(all_majors))
        return {
            "available": True,
            "status": "ok",
            "ok": True,
            "path": PRESETS_FILENAME,
            "version": data.get("version"),
            "min_firefox_version": data.get("min_firefox_version"),
            "generated_at": data.get("generated_at"),
            "counts": counts,
            "sample_ua": sample_ua,
            "ua_major": ua_major,
            "all_ua_majors": uniq_majors,
            "total_presets": sum(counts.values()) if counts else 0,
        }
    except Exception as e:
        # best-effort skipped if camoufox not installed
        msg = str(e)[:300]
        # Detect "No such file" vs import error -> skipped
        return {
            "available": False,
            "status": "skipped",
            "ok": True,
            "path": PRESETS_FILENAME,
            "version": None,
            "min_firefox_version": None,
            "generated_at": None,
            "counts": {},
            "sample_ua": None,
            "ua_major": None,
            "all_ua_majors": [],
            "total_presets": 0,
            "message": f"presets check skipped (camoufox not installed or file missing): {msg}",
        }

def _build_ua_check(ua_major: Optional[int], expected: int = EXPECTED_CHROME_MAJOR) -> dict[str, Any]:
    if ua_major is None:
        return {
            "ua_major": None,
            "expected": expected,
            "outdated": False,
            "status": "skipped",
            "ok": True,
            "message": "UA version cannot be determined — skipped (camoufox not installed or no presets)",
        }
    outdated = ua_major < expected
    return {
        "ua_major": ua_major,
        "expected": expected,
        "outdated": outdated,
        "status": "outdated" if outdated else "ok",
        "ok": not outdated,
        "message": f"UA rv:{ua_major} vs expected {expected} → {'outdated' if outdated else 'ok'}",
    }

def build_report(expected_chrome: int = EXPECTED_CHROME_MAJOR) -> dict[str, Any]:
    """
    Строит отчёт kernel_check. Никогда не бросает исключение; отсутствующие компоненты → skipped.
    Возвращает dict с ключами: timestamp, expected_chrome, playwright, camoufox, kernel, presets, ua, environment, outdated, overall
    """
    ts = datetime.now(timezone.utc).isoformat()

    # environment from patches
    try:
        from core.patches import check_environment

        env = check_environment()
    except Exception as e:
        env = {"overall": "skipped", "error": str(e)[:300], "status": "skipped"}

    playwright = get_playwright_info()
    camoufox = get_camoufox_info()
    kernel = get_kernel_info()
    presets = get_presets_info()

    # UA vs Chrome check
    ua_major = presets.get("ua_major")
    ua_check = _build_ua_check(ua_major, expected=expected_chrome)

    # kernel vs presets check (informational, not for outdated flag but for report)
    kernel_major = kernel.get("major")
    min_ff = presets.get("min_firefox_version")
    kernel_vs_presets: dict[str, Any]
    if kernel_major is not None and min_ff is not None:
        # kernel should be >= min_firefox_version, else presets ahead of kernel
        if kernel_major < min_ff:
            kernel_vs_presets = {
                "status": "warn",
                "ok": False,
                "message": f"kernel {kernel_major} < presets min_firefox_version {min_ff} — presets ahead",
                "kernel_major": kernel_major,
                "min_firefox_version": min_ff,
            }
        else:
            kernel_vs_presets = {
                "status": "ok",
                "ok": True,
                "message": f"kernel {kernel_major} >= presets min {min_ff} ok",
                "kernel_major": kernel_major,
                "min_firefox_version": min_ff,
            }
    else:
        kernel_vs_presets = {
            "status": "skipped",
            "ok": True,
            "message": "kernel vs presets check skipped (missing data)",
            "kernel_major": kernel_major,
            "min_firefox_version": min_ff,
        }

    # Determine outdated flag: UA outdated OR kernel major < expected
    outdated = bool(ua_check.get("outdated"))
    if kernel_major is not None and kernel_major < expected_chrome:
        outdated = True
        # augment ua_check message
        ua_check["kernel_outdated"] = True

    # Determine overall
    # overall in {ok,warn,skipped} for normal case; outdated adds flag but overall stays warn/ok etc unless outdated
    # To keep test expectation (overall in {ok,warn,skipped}) we keep overall as environment overall unless outdated where we set "outdated"
    # However to satisfy test when not outdated, overall must be in {ok,warn,skipped}
    # Provide both: overall + outdated boolean
    env_overall = env.get("overall", "ok")
    # env overall can be fail if playwright incompatible
    if env_overall == "fail":
        overall = "fail"
    elif outdated:
        # keep outdated as distinct but also test will see outdated if triggered
        # To keep test in {ok,warn,skipped} when not outdated, this branch not taken in normal case
        overall = "outdated"
    elif camoufox.get("status") == "skipped" and presets.get("status") == "skipped":
        # if major components skipped, still respect env overall which is warn
        # but if both skipped due to no camoufox, we could mark skipped if env is skipped?
        if env_overall in ("ok", "warn", "skipped"):
            overall = env_overall
        else:
            overall = "skipped"
    else:
        # normal: use env overall (typically warn due to canvas_seed)
        if env_overall in ("ok", "warn", "skipped", "fail"):
            overall = env_overall
        else:
            overall = "warn"

    report: dict[str, Any] = {
        "tool": "kernel_check",
        "timestamp": ts,
        "expected_chrome": expected_chrome,
        "expected_chrome_major": expected_chrome,
        "outdated": outdated,
        "overall": overall,
        "playwright": playwright,
        "camoufox": camoufox,
        "kernel": kernel,
        "presets": presets,
        "ua": ua_check,
        "kernel_vs_presets": kernel_vs_presets,
        "environment": env,
        "beta_version": kernel.get("beta_version"),
        "checks": {
            "playwright": playwright.get("status"),
            "camoufox": camoufox.get("status"),
            "presets": presets.get("status"),
            "ua": ua_check.get("status"),
            "kernel_vs_presets": kernel_vs_presets.get("status"),
            "environment": env_overall,
        },
    }
    return report

def main(argv: Optional[list[str]] = None) -> None:
    ap = argparse.ArgumentParser(description="AgentFox kernel staleness check (H7/H10) — cron-able")
    ap.add_argument("--json", dest="json_path", default=None, help="путь для JSON-отчёта")
    ap.add_argument(
        "--fail-on",
        choices=["outdated", "any", "none"],
        default="outdated",
        help="когда exit 1: outdated = если kernel устарел (UA < expected), any = если outdated или fail, none = никогда",
    )
    ap.add_argument("--expected", type=int, default=EXPECTED_CHROME_MAJOR, help=f"ожидаемая версия Chrome/Firefox (default {EXPECTED_CHROME_MAJOR})")
    args = ap.parse_args(argv)

    report = build_report(expected_chrome=args.expected)

    # pretty stdout
    print(f"[kernel-check] {report['timestamp']} expected={args.expected} overall={report['overall']} outdated={report['outdated']}")
    print(f"  playwright: {report['playwright'].get('version')} status={report['playwright'].get('status')} ok={report['playwright'].get('ok')} ({report['playwright'].get('message','')[:100]})")
    print(f"  camoufox: {report['camoufox'].get('version')} status={report['camoufox'].get('status')} ({report['camoufox'].get('message','')[:100]})")
    print(f"  kernel: {report['kernel'].get('beta_version')} major={report['kernel'].get('major')}")
    print(f"  presets: {report['presets'].get('path')} status={report['presets'].get('status')} version={report['presets'].get('version')} min_ff={report['presets'].get('min_firefox_version')} counts={report['presets'].get('counts')} sample_ua={str(report['presets'].get('sample_ua') or '')[:80]} ua_major={report['presets'].get('ua_major')} all_majors={report['presets'].get('all_ua_majors')}")
    print(f"  UA vs expected: {report['ua'].get('ua_major')} vs {report['ua'].get('expected')} outdated={report['ua'].get('outdated')} status={report['ua'].get('status')}")
    print(f"  kernel vs presets: {report['kernel_vs_presets'].get('message')}")
    print(f"  environment overall: {report['environment'].get('overall')}")

    if args.json_path:
        p = Path(args.json_path)
        p.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[kernel-check] JSON written to {p} ({p.stat().st_size} bytes)")

    # exit code based on --fail-on
    if args.fail_on == "none":
        sys.exit(0)
    elif args.fail_on == "outdated":
        sys.exit(1 if report.get("outdated") else 0)
    elif args.fail_on == "any":
        should_fail = bool(report.get("outdated") or report.get("overall") in ("fail", "outdated"))
        sys.exit(1 if should_fail else 0)
    else:
        sys.exit(1 if report.get("outdated") else 0)

# aliases for tests / backward compat
check_kernel = build_report
get_report = build_report
run_check = build_report

if __name__ == "__main__":
    main()
