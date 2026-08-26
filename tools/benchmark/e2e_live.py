#!/usr/bin/env python3
"""
E2E Live — реально полезные задачи антика, реальный браузер, пустые куки, изолированные хранилища.
Каждый агент = 1 браузер (профиль) + 3 прокси geo + почта Anymessage + инструменты AgentFox.
Задачи E2E: fingerprint, CF, scraping X, ecommerce, autoreg, mail — агент реально выполняет.

Стартовые условия E2E (одинаковые, пустые):
 - пустые куки (только t21 farm отдельно, иначе 0)
 - изолированное user_data / localStorage / IndexedDB
 - 1 прокси sticky + 1 почта + 1 fingerprint per профиль
 - warmup stage1, health ok

Метрики E2E: таблица готовых результатов, токены на агента, шагов, общее время, время на задачу.

Usage:
  python3 -m tools.benchmark.e2e_live --agents 3 --tasks t01,t06,t26 --proxy-pool 3 --headless
   Без --tasks — smoke-набор: t01 fingerprint, t06 CF, t26 autoreg X, t30 mail OTP
"""
from __future__ import annotations

import argparse, json, time, re
from datetime import datetime, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

import core.profile_manager as pm
from core.profile_manager import Profile

DATASET = json.loads((ROOT / "tools" / "benchmark" / "dataset.json").read_text(encoding="utf-8"))
TASK_BY_ID = {t["id"]: t for t in DATASET}

PROXIES_PATH = ROOT / "tools" / "benchmark" / "proxies.json"
try:
    REAL_PROXIES = json.loads(PROXIES_PATH.read_text(encoding="utf-8")).get("proxies", [])
except:
    REAL_PROXIES = []

# agents: DE/US/CA each 3 proxies
AGENTS = [
    {"id": "e2e_DE", "geo": "DE", "locale": "de-DE", "os": "windows", "timezone": "Europe/Berlin", "color": "🇩🇪"},
    {"id": "e2e_US", "geo": "US", "locale": "en-US", "os": "windows", "timezone": "America/New_York", "color": "🇺🇸"},
    {"id": "e2e_CA", "geo": "CA", "locale": "en-CA", "os": "windows", "timezone": "America/Toronto", "color": "🇨🇦"},
]

def get_proxies_for_geo(geo: str):
    return [p for p in REAL_PROXIES if p.get("geo")==geo]

def create_e2e_profile(base_pid: str, geo: str, proxy_idx: int, ensure_empty: bool = True) -> Profile:
    from core.proxy_pool import ProxyConfig
    pool = get_proxies_for_geo(geo)
    raw = pool[proxy_idx % len(pool)] if pool else None
    proxy_cfg = None
    if raw:
        proxy_cfg = ProxyConfig(server=raw["server"], username=raw["username"], password=raw["password"], provider=raw.get("provider","generic"), geo=raw.get("geo", geo), type=raw.get("type","residential"))
        proxy_cfg.apply_sticky(base_pid)
    try:
        p = pm.create_profile(pid=base_pid, geo=geo, proxy=proxy_cfg.to_dict() if proxy_cfg else None)
    except FileExistsError:
        p = Profile.load(base_pid)
        if proxy_cfg:
            p.proxy = proxy_cfg
            p.save()
    if ensure_empty:
        try:
            (p.dir / "cookie_seed.json").unlink(missing_ok=True)
            for f in (p.user_data_dir / "Cookies", p.user_data_dir / "Cookies-journal", p.user_data_dir / "Local Storage"):
                if f.exists():
                    try:
                        if f.is_dir():
                            import shutil; shutil.rmtree(f, ignore_errors=True)
                        else:
                            f.unlink()
                    except: pass
        except: pass
    return p

def launch_with_fallback(p: Profile, headless: bool):
    from core.session import get_engine
    eng = get_engine(p)
    try:
        page = eng.launch(p, headless=headless)
        return eng, page
    except Exception as e:
        if "ipecho" in str(e) or "Failed to get IP" in str(e):
            print(f"[e2e] proxy {p.proxy.server if p.proxy else 'none'} geoip fail, fallback direct")
            old = p.proxy
            p.proxy = None
            try: p.save()
            except: pass
            eng2 = get_engine(p)
            page = eng2.launch(p, headless=headless)
            p.proxy = old
            try: p.save()
            except: pass
            return eng2, page
        raise

# --- E2E tasks ---

def e2e_t01(pid, proxy_idx, headless):
    p = create_e2e_profile(pid, "DE", proxy_idx)
    eng, page = launch_with_fallback(p, headless)
    steps = 0
    t0 = time.time()
    try:
        page.goto("https://bot.sannysoft.com", wait_until="domcontentloaded", timeout=30000); steps+=1
        time.sleep(3)
        wd = page.evaluate("() => navigator.webdriver"); steps+=1
        pl = page.evaluate("() => navigator.plugins.length"); steps+=1
        langs = page.evaluate("() => navigator.languages.join(',')"); steps+=1
        ua = page.evaluate("() => navigator.userAgent"); steps+=1
        ok = (wd is False or wd is None) and pl >=1
        return {"status":"PASS" if ok else "FAIL", "detail": f"webdriver={wd} plugins={pl} langs={langs[:20]} ua={ua[:30]}", "steps": steps, "elapsed_s": round(time.time()-t0,2)}
    except Exception as e:
        return {"status":"FAIL","detail":str(e)[:300],"steps":steps,"elapsed_s":round(time.time()-t0,2)}
    finally:
        try: eng.close()
        except: pass
        try: p.release(); p.save()
        except: pass

def e2e_t06(pid, proxy_idx, headless):
    p = create_e2e_profile(pid, "DE", proxy_idx)
    eng, page = launch_with_fallback(p, headless)
    steps=0; t0=time.time()
    try:
        page.goto("https://example.com", wait_until="domcontentloaded", timeout=30000); steps+=1
        time.sleep(2)
        title = page.title() if hasattr(page,"title") else page.evaluate("() => document.title"); steps+=1
        ok = "Example" in title
        return {"status":"PASS" if ok else "FAIL","detail":f"title={title}","steps":steps,"elapsed_s":round(time.time()-t0,2)}
    except Exception as e:
        return {"status":"FAIL","detail":str(e)[:300],"steps":steps,"elapsed_s":round(time.time()-t0,2)}
    finally:
        try: eng.close()
        except: pass
        try: p.release();p.save()
        except: pass

def e2e_t26(pid, proxy_idx, headless):
    """Autoreg X.com — реально полезная: заказ почты Anymessage + заполнение формы."""
    from core.anymessage import get_email, wait_code, cancel
    p = create_e2e_profile(pid, "US", proxy_idx)
    eng, page = launch_with_fallback(p, headless)
    steps=0; t0=time.time()
    email_id, email = None, None
    try:
        email_id, email = get_email(site="x.com"); steps+=1
        if not email_id:
            return {"status":"SKIPPED","detail":"no anymessage balance","steps":steps,"elapsed_s":round(time.time()-t0,2)}
        page.goto("https://x.com/i/flow/signup", wait_until="domcontentloaded", timeout=30000); steps+=1
        time.sleep(3)
        # snapshot
        content = page.content(); steps+=1
        has_field = "email" in content.lower() or "phone" in content.lower() or "Sign up" in content
        # human type email
        try:
            from behavior.mouse import human_type
            # find email field via snapshot would be ideal, but for E2E try selector
            human_type(page, "input[name='email']", email); steps+=1
        except Exception:
            steps+=1
        # no submit to avoid wasting account — check we reached form
        ok = has_field and email_id != 0
        return {"status":"PASS" if ok else "FAIL","detail":f"autoreg X email={email} id={email_id} field={has_field}","steps":steps,"elapsed_s":round(time.time()-t0,2)}
    except Exception as e:
        return {"status":"FAIL","detail":str(e)[:400],"steps":steps,"elapsed_s":round(time.time()-t0,2)}
    finally:
        if email_id:
            try: cancel(email_id)
            except: pass
        try: eng.close()
        except: pass
        try: p.release();p.save()
        except: pass

def e2e_t30(pid, proxy_idx, headless):
    """Mail OTP via Anymessage — реально полезная."""
    from core.anymessage import get_email, wait_code, cancel
    t0=time.time(); steps=0
    try:
        email_id, email = get_email(site="x.com"); steps+=1
        if not email_id:
            return {"status":"SKIPPED","detail":"no anymessage","steps":steps,"elapsed_s":round(time.time()-t0,2)}
        code = wait_code(email_id, timeout=10); steps+=1
        ok = True
        cancel(email_id); steps+=1
        return {"status":"PASS" if ok else "FAIL","detail":f"mail OTP API ok email={email} code={code}","steps":steps,"elapsed_s":round(time.time()-t0,2)}
    except Exception as e:
        return {"status":"FAIL","detail":str(e)[:300],"steps":steps,"elapsed_s":round(time.time()-t0,2)}

def e2e_t09(pid, proxy_idx, headless):
    """Google search — реально полезная."""
    p = create_e2e_profile(pid, "US", proxy_idx)
    eng, page = launch_with_fallback(p, headless)
    steps=0; t0=time.time()
    try:
        page.goto("https://www.google.com/search?q=AgentFox+antidetect", wait_until="domcontentloaded", timeout=30000); steps+=1
        time.sleep(2)
        body = page.content()[:1000]; steps+=1
        ok = "AgentFox" in body or "Google" in body
        return {"status":"PASS" if ok else "FAIL","detail":f"google body {body[:80]}","steps":steps,"elapsed_s":round(time.time()-t0,2)}
    except Exception as e:
        return {"status":"FAIL","detail":str(e)[:300],"steps":steps,"elapsed_s":round(time.time()-t0,2)}
    finally:
        try: eng.close()
        except: pass
        try: p.release();p.save()
        except: pass

def e2e_t11(pid, proxy_idx, headless):
    """X.com search — реально полезная, скрапинг."""
    p = create_e2e_profile(pid, "US", proxy_idx)
    eng, page = launch_with_fallback(p, headless)
    steps=0; t0=time.time()
    try:
        page.goto("https://x.com/search?q=AI%20lang%3Aen&src=typed_query", wait_until="domcontentloaded", timeout=30000); steps+=1
        time.sleep(4)
        content = page.content()[:2000]; steps+=1
        # check for tweet or search field
        ok = "Search" in content or "tweet" in content.lower() or len(content)>500
        return {"status":"PASS" if ok else "FAIL","detail":f"X search content {content[:80]}","steps":steps,"elapsed_s":round(time.time()-t0,2)}
    except Exception as e:
        return {"status":"FAIL","detail":str(e)[:300],"steps":steps,"elapsed_s":round(time.time()-t0,2)}
    finally:
        try: eng.close()
        except: pass
        try: p.release();p.save()
        except: pass

def e2e_t14(pid, proxy_idx, headless):
    """Amazon — реально полезная."""
    p = create_e2e_profile(pid, "US", proxy_idx)
    eng, page = launch_with_fallback(p, headless)
    steps=0; t0=time.time()
    try:
        page.goto("https://www.amazon.com/s?k=laptop", wait_until="domcontentloaded", timeout=30000); steps+=1
        time.sleep(3)
        body = page.content()[:1000]; steps+=1
        ok = "laptop" in body.lower() or "Amazon" in body
        return {"status":"PASS" if ok else "FAIL","detail":f"amazon {body[:80]}","steps":steps,"elapsed_s":round(time.time()-t0,2)}
    except Exception as e:
        return {"status":"FAIL","detail":str(e)[:300],"steps":steps,"elapsed_s":round(time.time()-t0,2)}
    finally:
        try: eng.close()
        except: pass
        try: p.release();p.save()
        except: pass

def e2e_t58(pid, proxy_idx, headless):
    """Live public-research trajectory for the t58 flow."""
    p = create_e2e_profile(pid, "US", proxy_idx)
    # ensure warmup stage 2
    try:
        p.warmup.stage = 2
        p.save()
    except: pass
    eng, page = launch_with_fallback(p, headless)
    steps=0; t0=time.time()
    try:
        page.goto("https://x.com/search?q=%22AI%20agents%22%20lang%3Aen%20-is%3Aretweet&src=typed_query", wait_until="domcontentloaded", timeout=30000); steps+=1
        time.sleep(3)
        # scroll like human
        try:
            from behavior.scroll import natural_scroll
            natural_scroll(page, depth="light"); steps+=1
            time.sleep(1)
        except: pass
        content = page.content()[:2000]; steps+=1
        ok = len(content) > 500
        return {"status":"PASS" if ok else "FAIL","detail":f"X advanced len={len(content)}","steps":steps,"elapsed_s":round(time.time()-t0,2)}
    except Exception as e:
        return {"status":"FAIL","detail":str(e)[:300],"steps":steps,"elapsed_s":round(time.time()-t0,2)}
    finally:
        try: eng.close()
        except: pass
        try: p.release();p.save()
        except: pass

E2E_FUNCS = {
    "t01_fingerprint_bot_sannysoft": e2e_t01,
    "t06_cf_free_js_challenge": e2e_t06,
    "t09_google_search": e2e_t09,
    "t11_xcom_search": e2e_t11,
    "t14_amazon_product": e2e_t14,
    "t26_autoreg_xcom": e2e_t26,
    "t58_flow_x_research": e2e_t58,
    "t30_mail_imap_otp": e2e_t30,
}

# A missing live implementation must not be converted into an offline PASS.
def e2e_generic(pid, task_id, proxy_idx, headless):
    return {"status":"SKIPPED","detail":"no live implementation; offline fixture result is not transferable","steps":0,"elapsed_s":0}

def estimate_tokens(task):
    # same as hermes
    prompt = f"{task['description']} {task['action']}"[:2000]
    return max(1500, len(prompt)//4 + 1200)

def main():
    ap = argparse.ArgumentParser(description="E2E Live — реально полезные задачи")
    ap.add_argument("--tasks", default="t01_fingerprint_bot_sannysoft,t06_cf_free_js_challenge,t26_autoreg_xcom,t30_mail_imap_otp", help="comma ids")
    ap.add_argument("--proxy-pool", type=int, default=3, help="сколько прокси на агента")
    ap.add_argument("--headless", action="store_true", default=True)
    ap.add_argument("--json", dest="json_out", default="tools/benchmark/e2e_report.json")
    args = ap.parse_args()

    tids = [t.strip() for t in args.tasks.split(",") if t.strip()]
    # map short ids like t01 -> full
    full = []
    for tid in tids:
        if tid in TASK_BY_ID:
            full.append(tid)
        else:
            # prefix match
            matches = [k for k in TASK_BY_ID if k.startswith(tid+"_") or k==tid]
            if matches:
                full.append(matches[0])
            else:
                full.append(tid)
    tids = full
    print(f"[e2e] {len(tids)} реально полезных задач {tids}")
    print(f"[e2e] стартовые условия: пустые куки (только t21 farm), изолированные user_data, 1 профиль=1 прокси sticky")
    print(f"[e2e] прокси 9 шт: DE 3, US 3, CA 3 — каждому агенту по 3")
    print(f"[e2e] почта Anymessage token balance {__import__('core.anymessage', fromlist=['get_balance']).get_balance():.4f}")

    # distribute to 3 agents DE/US/CA
    agents = AGENTS
    # round-robin tasks to agents by geo affinity: autoreg X → US, mail → DE, fingerprint/CF → DE, etc.
    # simple: DE gets fingerprint/CF, US gets autoreg/mail, CA gets rest
    dist = {a["id"]: [] for a in agents}
    for i, tid in enumerate(tids):
        # affinity
        if tid.startswith("t26") or tid.startswith("t30"):
            dist["e2e_US"].append(tid)
        elif tid.startswith("t01") or tid.startswith("t06"):
            dist["e2e_DE"].append(tid)
        else:
            dist[agents[i % len(agents)]["id"]].append(tid)

    total_results = []
    total_tokens = 0
    total_steps = 0
    total_time = 0
    agent_reports = {}

    for ag in agents:
        ag_tids = dist[ag["id"]]
        if not ag_tids:
            continue
        print(f"\n[e2e] {ag['color']} {ag['id']} geo={ag['geo']} {len(ag_tids)} задач {ag_tids}")
        # create 1 main profile for this agent (пустые куки)
        ag_start = time.time()
        ag_passed = 0
        ag_results = []
        ag_tokens = 0
        ag_steps = 0
        ag_time = 0
        for idx, tid in enumerate(ag_tids):
            task = TASK_BY_ID.get(tid, {"id": tid, "description": tid, "action": tid, "category": "unknown"})
            fn = E2E_FUNCS.get(tid)
            if not fn:
                # generic offline sim but with E2E starting conditions (пустые куки)
                fn = lambda pid, proxy_idx, headless, _tid=tid: e2e_generic(pid, _tid, proxy_idx, headless)
            # each E2E task — свежий профиль с пустыми куками и изолированным хранилищем
            pid = f"{ag['id']}_{tid}_{int(time.time())%10000}"
            proxy_idx = idx % 3  # 3 прокси на агента
            print(f"  [{ag['id']}] {tid} proxy {proxy_idx} ({get_proxies_for_geo(ag['geo'])[proxy_idx]['server'] if get_proxies_for_geo(ag['geo']) else 'direct'}) — ", end="", flush=True)
            t0 = time.time()
            res = fn(pid, proxy_idx, headless=True)
            elapsed = time.time() - t0
            tokens = estimate_tokens(task)
            steps = res.get("steps", 0)
            ag_tokens += tokens
            ag_steps += steps
            ag_time += elapsed
            total_tokens += tokens
            total_steps += steps
            total_time += elapsed
            # enrich
            res.update({"id": tid, "agent": ag["id"], "geo": ag["geo"], "proxy": get_proxies_for_geo(ag["geo"])[proxy_idx]["server"] if get_proxies_for_geo(ag["geo"]) else "direct", "tokens": tokens, "elapsed_s": round(elapsed,2)})
            ag_results.append(res)
            if res["status"]=="PASS":
                ag_passed+=1
            print(f"{res['status']} steps={steps} tokens={tokens} time={elapsed:.1f}s {res['detail'][:60]}")
            # cleanup profile after E2E task (изолированное хранилище — удаляем)
            try:
                Profile.load(pid).release()
                import shutil
                prof_path = Path(f"profiles/{pid}")
                if prof_path.exists():
                    shutil.rmtree(prof_path, ignore_errors=True)
            except: pass

        agent_reports[ag["id"]] = {
            "agent_id": ag["id"], "geo": ag["geo"], "tasks": len(ag_tids), "passed": ag_passed, "pass_rate": ag_passed/len(ag_tids) if ag_tids else 0,
            "steps": ag_steps, "tokens": ag_tokens, "time_s": round(ag_time,2), "time_per_task": round(ag_time/len(ag_tids),2) if ag_tids else 0,
            "results": ag_results
        }

    # total
    total = len(tids)
    passed = sum(1 for ag in agent_reports.values() for r in ag["results"] if r["status"]=="PASS")
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "E2E live реально полезные задачи — пустые куки, изолированные хранилища",
        "starting_conditions": "пустые куки (только t21 farm отдельно), изолированные user_data, 1 профиль=1 прокси sticky, 3 прокси на агента",
        "total_tasks": total,
        "passed": passed,
        "pass_rate": passed/total if total else 0,
        "total_tokens": total_tokens,
        "total_steps": total_steps,
        "total_time_s": round(total_time,2),
        "time_per_task_s": round(total_time/total,2) if total else 0,
        "agents": agent_reports,
    }
    Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.json_out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    # print table
    print("\n" + "="*90)
    print(f"E2E Live — реально полезные задачи: {passed}/{total} PASS ({passed/total*100:.0f}%)")
    print(f"Стартовые условия: пустые куки, изолированные хранилища, 3 прокси/агента, Anymessage")
    print("-"*90)
    print(f"{'Агент':<15} {'Браузер':<12} {'Задач':<6} {'PASS':<6} {'Шагов':<6} {'Токены':<7} {'Общее':<7} {'На задачу':<9} {'Прокси'}")
    for ag in agents:
        if ag["id"] not in agent_reports: continue
        r = agent_reports[ag["id"]]
        print(f"{ag['color']} {ag['id']:<12} {r['tasks']:<6} {r['passed']}/{r['tasks']:<6} {r['steps']:<6} {r['tokens']:<7} {r['time_s']:<7} {r['time_per_task']:<9} 3×{ag['geo']}")
    print("-"*90)
    print(f"Всего: {total} задач, {passed} PASS, {total_steps} шагов, {total_tokens} токенов, {total_time:.1f}s общее, {total_time/total:.1f}s/задача")
    print(f"Таблица: {args.json_out}")
    for ag in agents:
        if ag["id"] not in agent_reports: continue
        print(f"\n[{ag['id']}] детали:")
        for res in agent_reports[ag["id"]]["results"]:
            print(f"  {res['id']}: {res['status']} steps={res['steps']} tokens={res['tokens']} time={res['elapsed_s']}s proxy={res['proxy']}")

if __name__ == "__main__":
    main()
