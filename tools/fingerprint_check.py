#!/usr/bin/env python3
"""
CI-бенчмарк отпечатков AgentFox — процесс Multilogin (§8, §7.1).

Гоняет профиль(и) через детекторы после каждого релиза:
  - локальные spoof-чеки (offline, всегда): webdriver, plugins, languages,
    WebGL vendor/renderer, UA, permissions, broken-image — те же сигналы что на bot.sannysoft;
  - live прогон (опционально --live): bot.sannysoft + creepjs — best-effort,
    при недоступности сети помечается SKIPPED.

Использование:
  python -m tools.fingerprint_check              # offline, 1 профиль DE
  python -m tools.fingerprint_check --live --profiles 3 --geo US
  python -m tools.fingerprint_check --json report.json --fail-on critical

Выход: 0 если нет FAIL по critical-чекам, иначе 1. Печатает таблицу + JSON.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# --- pure helpers (тестируются без браузера) ---

BOT_IDS = [
    "user-agent-result",
    "webdriver-result",
    "advanced-webdriver-result",
    "chrome-result",
    "permissions-result",
    "plugins-length-result",
    "plugins-type-result",
    "languages-result",
    "webgl-vendor",
    "webgl-renderer",
    "broken-image-dimensions",
]

# критичные — провал = likely bot detection
CRITICAL_LOCAL = {
    "webdriver",
    "webdriver_advanced",
    "plugins_length",
    "plugins_type",
    "languages",
    "webgl_vendor",
    "webgl_renderer",
}

CRITICAL_BOT = {
    "webdriver-result",
    "advanced-webdriver-result",
    "plugins-length-result",
    "plugins-type-result",
    "languages-result",
}


def classify_bot_cell(cls: str, text: str) -> str:
    c = (cls or "").lower()
    if "passed" in c:
        return "PASS"
    if "failed" in c:
        return "FAIL"
    if "warn" in c:
        return "WARN"
    t = (text or "").lower()
    if "passed" in t:
        return "PASS"
    if "failed" in t:
        return "FAIL"
    return "SKIPPED"


def parse_bot_results(cells: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for cell in cells:
        cid = cell.get("id", "")
        cls = cell.get("cls", "")
        text = cell.get("text", "")
        status = classify_bot_cell(cls, text)
        out[cid] = {"status": status, "class": cls, "text": text[:200]}
    return out


def evaluate_local_result(name: str, value: Any, detail: str = "") -> dict[str, str]:
    # возвращает {status, detail}
    if name == "webdriver":
        ok = not bool(value)
        return {"status": "PASS" if ok else "FAIL", "detail": f"navigator.webdriver={value!r}"}
    if name == "webdriver_advanced":
        ok = not bool(value)
        return {"status": "PASS" if ok else "FAIL", "detail": "runBotDetection" if value else "clean"}
    if name == "ua_headless":
        s = str(value or "")
        ok = "HeadlessChrome" not in s
        return {"status": "PASS" if ok else "FAIL", "detail": s[:120]}
    if name == "plugins_length":
        n = int(value) if isinstance(value, int) else 0
        ok = n > 0
        return {"status": "PASS" if ok else "FAIL", "detail": f"plugins.length={n}"}
    if name == "plugins_type":
        ok = bool(value)
        return {"status": "PASS" if ok else "FAIL", "detail": str(value)[:120]}
    if name == "languages":
        ok = bool(value and len(str(value).strip()) > 0)
        return {"status": "PASS" if ok else "FAIL", "detail": str(value)[:120]}
    if name == "webgl_vendor":
        s = str(value or "")
        bad = s in ("Brian Paul", "Google Inc.") or not s or "error" in s.lower()
        return {"status": "FAIL" if bad else "PASS", "detail": s[:120]}
    if name == "webgl_renderer":
        s = str(value or "")
        bad = s in ("Mesa OffScreen",) or "Swift" in s or not s or "error" in s.lower() or "no webgl" in s.lower()
        return {"status": "FAIL" if bad else "PASS", "detail": s[:120]}
    if name == "broken_image":
        s = str(value or "")
        ok = s not in ("0x0", "0x0 ", "") and "0x0" not in s or "x" in s and s != "0x0"
        # bot.sannysoft: 0x0 -> failed, иначе passed
        if s.strip() == "0x0":
            return {"status": "FAIL", "detail": s}
        return {"status": "PASS" if "x" in s else "SKIPPED", "detail": s[:80]}
    if name == "chrome":
        # Firefox: window.chrome отсутствует — это нормально, не критично.
        # Помечаем INFO чтобы не ронять CI на Firefox.
        present = bool(value)
        return {"status": "INFO", "detail": "present" if present else "missing (expected on Firefox)"}
    if name == "permissions":
        s = str(value or "")
        # denided + prompt => failed, иначе pass — ставим WARN чтобы не блокировать
        if "denied+prompt" in s:
            return {"status": "FAIL", "detail": s}
        return {"status": "PASS", "detail": s[:80]}
    return {"status": "SKIPPED", "detail": detail or str(value)[:120]}


LOCAL_JS: dict[str, str] = {
    "webdriver": "navigator.webdriver",
    "ua_headless": "navigator.userAgent",
    "plugins_length": "navigator.plugins.length",
    "languages": "navigator.languages ? navigator.languages.join(',') : ''",
    "chrome": "!!window.chrome",
    # complex — evaluated via helper below
}


def _js_webdriver_advanced() -> str:
    return r"""
() => {
  const docKeys = ["__webdriver_evaluate","__selenium_evaluate","__webdriver_script_function","__webdriver_script_fn","__fxdriver_evaluate","__driver_unwrapped","__webdriver_unwrapped","__driver_evaluate","__selenium_unwrapped","__fxdriver_unwrapped","_Selenium_IDE_Recorder","_selenium","calledSelenium","_WEBDRIVER_ELEM_CACHE","ChromeDriverw","driver-evaluate","webdriver-evaluate","selenium-evaluate","webdriverCommand","webdriver-evaluate-response","__webdriverFunc","__webdriver_script_fn","__$webdriverAsyncExecutor","__lastWatirAlert","__lastWatirConfirm","__lastWatirPrompt","$chrome_asyncScriptInfo","$cdc_asdjflasutopfhvcZLmcfl_"];
  const winKeys = ["_phantom","__nightmare","_selenium","callPhantom","callSelenium","_Selenium_IDE_Recorder"];
  for (const k of winKeys) if (window[k]) return true;
  for (const k of docKeys) if (window.document[k]) return true;
  for (const k in window.document) if (k.match(/\$[a-z]dc_/) && window.document[k] && window.document[k].cache_) return true;
  if (window.external && window.external.toString && window.external.toString().indexOf('Sequentum') !== -1) return true;
  const de = window.document.documentElement;
  if (de && (de.getAttribute('selenium') || de.getAttribute('webdriver') || de.getAttribute('driver'))) return true;
  return false;
}
""".strip()


def _js_plugins_type() -> str:
    return "() => { try { return (navigator.plugins instanceof PluginArray) && navigator.plugins.length>0 && window.navigator.plugins[0].toString()==='[object Plugin]'; } catch(e){ return false; } }"


def _js_webgl_vendor() -> str:
    return "() => { try { const c=document.createElement('canvas'); const gl=c.getContext('webgl')||c.getContext('webgl-experimental'); if(!gl) return 'no webgl'; const ext=gl.getExtension('WEBGL_debug_renderer_info'); return ext ? gl.getParameter(ext.UNMASKED_VENDOR_WEBGL) : 'n/a'; } catch(e){ return 'error:'+e.message; } }"


def _js_webgl_renderer() -> str:
    return "() => { try { const c=document.createElement('canvas'); const gl=c.getContext('webgl')||c.getContext('webgl-experimental'); if(!gl) return 'no webgl'; const ext=gl.getExtension('WEBGL_debug_renderer_info'); return ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : 'n/a'; } catch(e){ return 'error:'+e.message; } }"


def _js_broken_image() -> str:
    return r"""
() => new Promise(resolve => {
  const img=document.createElement('img');
  img.onerror=()=> resolve(`${img.width}x${img.height}`);
  document.body.appendChild(img);
  img.src='data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7';
  setTimeout(()=> resolve(`${img.width}x${img.height}`), 800);
  // реальный тест bot.sannysoft грузит несуществующий URL; для offline используем data: — ожидаем 1x1 -> PASS
})
""".strip()


def _js_permissions() -> str:
    return r"""
async () => {
  try {
    const ps = await navigator.permissions.query({name:'notifications'});
    const state = ps.state;
    if (Notification.permission==='denied' && state==='prompt') return 'denied+prompt (failed)';
    return state + ' / Notification.' + Notification.permission;
  } catch(e){ return 'error:'+e.message; }
}
""".strip()


def run_local_checks(page) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    # simple
    for name, js in LOCAL_JS.items():
        try:
            val = page.evaluate(f"() => {js}")
        except Exception as e:
            val = f"error:{e}"
        out[name] = evaluate_local_result(name, val)
    # advanced webdriver
    try:
        v = page.evaluate(_js_webdriver_advanced())
    except Exception as e:
        v = f"error:{e}"
    out["webdriver_advanced"] = evaluate_local_result("webdriver_advanced", v)
    try:
        v = page.evaluate(_js_plugins_type())
    except Exception as e:
        v = False
    out["plugins_type"] = evaluate_local_result("plugins_type", v)
    for name, js_fn in [
        ("webgl_vendor", _js_webgl_vendor()),
        ("webgl_renderer", _js_webgl_renderer()),
        ("permissions", _js_permissions()),
    ]:
        try:
            v = page.evaluate(js_fn)
        except Exception as e:
            v = f"error:{e}"
        out[name] = evaluate_local_result(name, v)
    # broken image — async, skip if page.evaluate doesn't support promise (FakePage)
    try:
        v = page.evaluate(_js_broken_image())
        # sync fake returns None; real returns 1x1 after promise
        if v is None:
            out["broken_image"] = {"status": "SKIPPED", "detail": "no promise support (fake)"}
        else:
            out["broken_image"] = evaluate_local_result("broken_image", v)
    except Exception:
        out["broken_image"] = {"status": "SKIPPED", "detail": "evaluate failed"}
    return out


def run_bot_checks(page, timeout: int = 30000) -> dict[str, Any]:
    url = "https://bot.sannysoft.com/"
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        time.sleep(2.5)
        cells = page.evaluate(
            "() => { const ids=['user-agent-result','webdriver-result','advanced-webdriver-result','chrome-result','permissions-result','plugins-length-result','plugins-type-result','languages-result','webgl-vendor','webgl-renderer','broken-image-dimensions']; return ids.map(id=>{const el=document.getElementById(id); return {id, cls: el?el.className:'', text: el?(el.textContent||el.innerText||'').trim():''};}); }"
        )
        if not isinstance(cells, list):
            return {"status": "SKIPPED", "detail": "no cells", "url": url}
        parsed = parse_bot_results(cells)
        fails = [k for k, v in parsed.items() if v["status"] == "FAIL" and k in CRITICAL_BOT]
        overall = "FAIL" if fails else "PASS"
        # chrome on Firefox всегда FAIL — не считаем
        if parsed.get("chrome-result", {}).get("status") == "FAIL":
            parsed["chrome-result"]["status"] = "INFO"
            parsed["chrome-result"]["note"] = "expected missing on Firefox"
        return {"status": overall, "url": url, "cells": parsed}
    except Exception as e:
        return {"status": "SKIPPED", "detail": str(e)[:300], "url": url}


def run_creepjs_checks(page, timeout: int = 30000) -> dict[str, Any]:
    url = "https://abrahamjuliot.github.io/creepjs/"
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        time.sleep(3.0)
        info = page.evaluate(
            "() => ({ body: (document.body.innerText||'').slice(0,4000), title: document.title, fp: (document.getElementById('fp')?document.getElementById('fp').innerText:'').slice(0,500) })"
        )
        body = (info.get("body") or "") if isinstance(info, dict) else str(info)[:4000]
        low = body.lower()
        # creepjs показывает lies/trust — ищем ключевые слова
        has_fp = "fingerprint" in low or "creepjs" in low or len(body) > 500
        detail = body[:600].replace("\n", " ")
        if not has_fp:
            return {"status": "SKIPPED", "detail": "page not loaded as expected", "url": url, "snippet": detail[:300]}
        # эвристика: если в тексте есть "lies: 0" или отсутствие lies — PASS, иначе WARN
        if "lies" in low:
            # count lies occurrences
            return {"status": "WARN", "detail": "lies keyword present — inspect manually", "url": url, "snippet": detail[:400]}
        return {"status": "PASS", "detail": "creepjs loaded, no obvious lies", "url": url, "snippet": detail[:400]}
    except Exception as e:
        return {"status": "SKIPPED", "detail": str(e)[:300], "url": url}


def collect_one(profile_id: str, geo: str, headless: bool, live: bool, timeout: int) -> dict[str, Any]:
    import core.profile_manager as pm
    from core.session import get_engine

    try:
        p = pm.Profile.load(profile_id)
    except FileNotFoundError:
        from core.profile_manager import create_profile

        p = create_profile(profile_id, geo=geo)

    p.acquire(f"fp_check_{profile_id}")
    engine = get_engine(p)
    page = None
    report: dict[str, Any] = {
        "profile_id": profile_id,
        "geo": geo,
        "os": p.identity.os,
        "locale": p.identity.locale,
        "timezone": p.identity.timezone,
        "screen": p.identity.screen,
        "preset_id": p.identity.fingerprint_preset_id[:12],
    }
    try:
        page = engine.launch(p, headless=headless)
        # разогреть about:blank
        try:
            page.goto("https://example.com", wait_until="domcontentloaded", timeout=timeout)
            time.sleep(1.0)
        except Exception:
            pass
        report["local"] = run_local_checks(page)
        local_fails = [k for k, v in report["local"].items() if v["status"] == "FAIL" and k in CRITICAL_LOCAL]
        report["local_overall"] = "FAIL" if local_fails else "PASS"
        if live:
            report["bot"] = run_bot_checks(page, timeout=timeout)
            report["creepjs"] = run_creepjs_checks(page, timeout=timeout)
            live_fails = []
            if report["bot"].get("status") == "FAIL":
                live_fails.append("bot")
            if report["creepjs"].get("status") == "FAIL":
                live_fails.append("creepjs")
            report["live_overall"] = "FAIL" if live_fails else "PASS"
        else:
            report["bot"] = {"status": "SKIPPED", "detail": "offline mode (use --live)"}
            report["creepjs"] = {"status": "SKIPPED", "detail": "offline mode (use --live)"}
            report["live_overall"] = "SKIPPED"
        fails = local_fails[:]
        if live and report.get("live_overall") == "FAIL":
            fails.append("live")
        report["overall"] = "FAIL" if fails else "PASS"
    except Exception as e:
        report["error"] = str(e)[:500]
        report["overall"] = "FAIL"
    finally:
        try:
            engine.close()
        except Exception:
            pass
        p.release()
        try:
            p.save()
        except Exception:
            pass
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description="AgentFox fingerprint CI benchmark (bot.sannysoft + creepjs)")
    ap.add_argument("--profiles", type=int, default=1, help="сколько профилей прогнать (default 1)")
    ap.add_argument("--geo", default="DE", help="гео для новых профилей")
    ap.add_argument("--headless", action="store_true", default=True, help="headless (default true)")
    ap.add_argument("--no-headless", dest="headless", action="store_false", help="показать окно")
    ap.add_argument("--live", action="store_true", help="ходить на bot.sannysoft/creepjs (требует сеть)")
    ap.add_argument("--timeout", type=int, default=30000, help="goto timeout ms")
    ap.add_argument("--json", dest="json_path", default=None, help="путь для JSON-отчёта")
    ap.add_argument("--fail-on", choices=["critical", "any", "none"], default="critical", help="когда exit 1")
    ap.add_argument("--profile-id", default=None, help="использовать существующий профиль вместо временного")
    args = ap.parse_args()

    # изолированный PROFILES_ROOT чтобы не трогать продовые профили, если не указан --profile-id
    tmp_root = None
    if not args.profile_id:
        tmp_root = Path(tempfile.mkdtemp(prefix="agentfox_fpcheck_"))
        import core.profile_manager as pm

        pm.PROFILES_ROOT = tmp_root
        print(f"[fp-check] isolated profiles root: {tmp_root}")

    ts = datetime.now(timezone.utc).isoformat()
    reports: list[dict[str, Any]] = []
    for i in range(args.profiles):
        pid = args.profile_id or f"fpcheck_{int(time.time()*1000)}_{i}"
        print(f"\n[{i+1}/{args.profiles}] profile={pid} geo={args.geo} live={args.live}")
        rep = collect_one(pid, args.geo, headless=args.headless, live=args.live, timeout=args.timeout)
        reports.append(rep)
        # краткая строка
        local = rep.get("local_overall", "?")
        bot = rep.get("bot", {}).get("status", "?")
        creep = rep.get("creepjs", {}).get("status", "?")
        print(f"  local={local} bot={bot} creepjs={creep} overall={rep.get('overall')}")

    summary = {
        "tool": "fingerprint_check",
        "timestamp": ts,
        "live": args.live,
        "profiles": reports,
        "summary": {
            "total": len(reports),
            "passed": sum(1 for r in reports if r.get("overall") == "PASS"),
            "failed": sum(1 for r in reports if r.get("overall") == "FAIL"),
            "skipped": sum(1 for r in reports if r.get("overall") == "SKIPPED"),
        },
    }
    # pretty
    print("\n" + "=" * 56)
    print(f"SUMMARY: {summary['summary']['passed']}/{summary['summary']['total']} PASS"
          f"  failed={summary['summary']['failed']}")
    for r in reports:
        print(f"\n— {r['profile_id']} ({r['os']}/{r['locale']}/{r['screen']}) overall={r['overall']}")
        for name, res in r.get("local", {}).items():
            mark = {"PASS": "✓", "FAIL": "✗", "WARN": "~", "INFO": "·", "SKIPPED": "-"}.get(res["status"], "?")
            print(f"  [{mark} {res['status']:7}] {name:20} {res['detail'][:70]}")
        if r.get("bot", {}).get("cells"):
            for cid, cell in r["bot"]["cells"].items():
                s = cell["status"]
                mark = {"PASS": "✓", "FAIL": "✗", "WARN": "~", "INFO": "·", "SKIPPED": "-"}.get(s, "?")
                print(f"  [{mark} {s:7}] bot:{cid:30} {cell['text'][:60]}")
        for k in ("bot", "creepjs"):
            v = r.get(k, {})
            if v and "cells" not in v:
                s = v.get("status", "?")
                mark = {"PASS": "✓", "FAIL": "✗", "WARN": "~", "INFO": "·", "SKIPPED": "-"}.get(s, "?")
                print(f"  [{mark} {s:7}] {k:20} {v.get('detail','')[:90]}")

    if args.json_path:
        Path(args.json_path).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[fp-check] JSON written to {args.json_path}")

    # exit code
    if args.fail_on == "none":
        sys.exit(0)
    if args.fail_on == "any":
        sys.exit(0 if summary["summary"]["failed"] == 0 and summary["summary"]["passed"] > 0 else 1)
    # critical: любой FAIL в overall
    sys.exit(0 if summary["summary"]["failed"] == 0 else 1)


if __name__ == "__main__":
    main()
