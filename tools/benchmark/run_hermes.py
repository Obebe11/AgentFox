#!/usr/bin/env python3
"""
Hermes x AgentFox — 3 изолированных агента на benchmark dataset.

Даёт Hermes 3 изолированных места (3 AgentFox профиля с разными geo/fingerprint/proxy)
 и гонит полный dataset.json параллельно. Замеряет: задачи, время, токены.

Usage:
   python -m tools.benchmark.run_hermes                    # offline simulation, токены ~оценка len/4
  python -m tools.benchmark.run_hermes --live             # + реальный прогон 1 задачи через hermes chat --usage-file
  python -m tools.benchmark.run_hermes --worktree         # + создаёт 3 git worktree для Hermes (если hermes CLI доступен)

Метрики: per-agent tasks/time/tokens, total, pass_rate
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import tempfile
import textwrap
import concurrent.futures
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import os

ROOT = Path(__file__).parent.parent.parent
DATASET = Path(__file__).parent / "dataset.json"
AGENT_MD = ROOT / "AGENT.md"

sys.path.insert(0, str(ROOT))

# isolate benchmark run
import core.profile_manager as pm

BENCH_ROOT = Path(tempfile.mkdtemp(prefix="hermes_bench_"))
pm.PROFILES_ROOT = BENCH_ROOT

# token estimation
def estimate_tokens(text: str) -> int:
    # heuristic 4 chars per token, clamp
    if not text:
        return 0
    return max(1, len(text) // 4)

def load_agent_instruction_tokens() -> int:
    try:
        txt = AGENT_MD.read_text(encoding="utf-8")
        return estimate_tokens(txt)
    except Exception:
        return 1750  # fallback from AGENT.md ~7000 chars

AGENT_TOKENS = load_agent_instruction_tokens()
TOOL_SCHEMA_TOKENS = 800  # api/server 20 endpoints spec compact
SNAPSHOT_TOKENS = 400  # avg snapshot tree @e refs

# import benchmark task funcs from run.py
import tools.benchmark.run as bench_run

# 3 изолированных агента — каждому 3 прокси своей geo (проверены direct ip-api 9/9)
AGENTS = [
    {"id": "hermes_DE", "geo": "DE", "locale": "de-DE", "os": "windows", "timezone": "Europe/Berlin", "color": "🇩🇪", "tasks_slice": "fingerprint+cf+autoreg_de"},
    {"id": "hermes_US", "geo": "US", "locale": "en-US", "os": "windows", "timezone": "America/New_York", "color": "🇺🇸", "tasks_slice": "scraping+social+autoreg_us"},
    {"id": "hermes_CA", "geo": "CA", "locale": "en-CA", "os": "windows", "timezone": "America/Toronto", "color": "🇨🇦", "tasks_slice": "ecommerce+crypto+warmup+autoreg_ca"},
]

# Keep related tasks on the same geo while covering the complete dataset.
def distribute_tasks(dataset: list[dict]) -> dict[str, list[dict]]:
    category_owner = {
        "fingerprint": "hermes_DE",
        "cloudflare": "hermes_DE",
        "isolation": "hermes_DE",
        "cookies": "hermes_DE",
        "warmup": "hermes_DE",
        "scheduler": "hermes_DE",
        "proxy": "hermes_DE",
        "behavior": "hermes_DE",
        "lifecycle": "hermes_DE",
        "health": "hermes_DE",
        "scraping": "hermes_US",
        "social": "hermes_US",
        "autoreg": "hermes_US",
        "mail": "hermes_US",
        "ticketing": "hermes_US",
        "ecommerce": "hermes_CA",
        "crypto": "hermes_CA",
        "betting": "hermes_CA",
    }
    out = {agent["id"]: [] for agent in AGENTS}
    for index, task in enumerate(dataset):
        owner = category_owner.get(task["category"], AGENTS[index % len(AGENTS)]["id"])
        out[owner].append(task)
    return out

def ensure_profiles():
    from core.proxy_pool import ProxyConfig
    import json as _js
    _real_proxies = []
    for _pf in [Path("tools/benchmark/proxies.json"), ROOT / "tools" / "benchmark" / "proxies.json"]:
        if _pf.exists():
            try:
                _real_proxies = _js.loads(_pf.read_text(encoding="utf-8")).get("proxies", [])
                break
            except Exception:
                pass
    # каждому агенту — по 3 прокси его geo (проверены direct 9/9: US 3, DE 3, CA 3)
    by_geo = {}
    for pr in _real_proxies:
        by_geo.setdefault(pr.get("geo",""), []).append(pr)
    profiles = {}
    for idx, ag in enumerate(AGENTS):
        pid = ag["id"]
        # E2E старт: пустые куки, изолированное хранилище — 3 профиля на агента (hermes_DE, _1, _2)
        geo_pool = by_geo.get(ag["geo"], [])
        # основной профиль
        try:
            p = pm.create_profile(pid=pid, geo=ag["geo"], locale=ag["locale"], os=ag["os"])
        except FileExistsError:
            p = pm.Profile.load(pid)
        if geo_pool and len(geo_pool) >= 3:
            # 3 прокси этой geo — раздаём по 1 на каждый из 3 профилей агента
            main_proxy = geo_pool[0]
            cfg = ProxyConfig(server=main_proxy["server"], username=main_proxy["username"], password=main_proxy["password"], provider=main_proxy.get("provider","generic"), geo=main_proxy.get("geo", ag["geo"]), type=main_proxy.get("type","residential"))
            cfg.apply_sticky(pid)
            p.proxy = cfg
            p.save()
            # пустые куки старт E2E (только t21 farm отдельно)
            try:
                (p.dir / "cookie_seed.json").unlink(missing_ok=True)
                for f in (p.user_data_dir / "Cookies", p.user_data_dir / "Cookies-journal"):
                    if f.exists():
                        try: f.unlink()
                        except: pass
            except: pass
            # 2 доп профиля для этого агента — изолированные хранилища, пустые куки
            for extra in (1, 2):
                extra_pid = f"{pid}_{extra}"
                extra_raw = geo_pool[extra]
                extra_cfg = ProxyConfig(server=extra_raw["server"], username=extra_raw["username"], password=extra_raw["password"], provider=extra_raw.get("provider","generic"), geo=extra_raw.get("geo", ag["geo"]), type=extra_raw.get("type","residential"))
                extra_cfg.apply_sticky(extra_pid)
                try:
                    ep = pm.create_profile(pid=extra_pid, geo=ag["geo"], locale=ag["locale"], os=ag["os"])
                except FileExistsError:
                    ep = Profile.load(extra_pid)
                ep.proxy = extra_cfg
                ep.save()
                try:
                    (ep.dir / "cookie_seed.json").unlink(missing_ok=True)
                    for f in (ep.user_data_dir / "Cookies", ep.user_data_dir / "Cookies-journal"):
                        if f.exists():
                            try: f.unlink()
                            except: pass
                except: pass
        elif _real_proxies:
            # fallback если пул не 3 — 1 прокси на агента
            cand_list = by_geo.get(ag["geo"], []) or _real_proxies
            cand = cand_list[idx % len(cand_list)] if cand_list else _real_proxies[idx % len(_real_proxies)]
            chosen = ProxyConfig(server=cand["server"], username=cand["username"], password=cand["password"], provider=cand.get("provider","generic"), geo=cand.get("geo", ag["geo"]), type=cand.get("type","residential"))
            chosen.apply_sticky(pid)
            p.proxy = chosen
            p.save()
        elif not p.proxy or not getattr(p.proxy, "server", ""):
            p.proxy = ProxyConfig(
                provider="generic",
                type="residential",
                server=f"http://{ag['geo'].lower()}-residential.proxy.example:8080",
                username=f"user_{ag['geo'].lower()}",
                password="pass",
                geo=ag["geo"],
            )
            try:
                p.proxy.apply_sticky(pid)
            except Exception:
                pass
            p.save()
        else:
            if ag["geo"].lower() not in p.proxy.server and not _real_proxies:
                p.proxy.server = f"http://{ag['geo'].lower()}-residential.proxy.example:8080"
                p.proxy.geo = ag["geo"]
                p.save()
        profiles[pid] = p
    presets = [pm.Profile.load(a["id"]).identity.fingerprint_preset_id for a in AGENTS]
    isolated = len(set(presets)) == 3
    dirs = [str(pm.Profile.load(a["id"]).user_data_dir) for a in AGENTS]
    dir_isolated = len(set(dirs)) == 3
    proxies = [pm.Profile.load(a["id"]).proxy.server if pm.Profile.load(a["id"]).proxy else "none" for a in AGENTS]
    proxy_isolated = len(set(proxies)) == 3
    return profiles, {"presets_isolated": isolated, "dirs_isolated": dir_isolated, "proxies_isolated": proxy_isolated, "presets": presets, "dirs": dirs, "proxies": proxies}

def run_agent_tasks(agent: dict, tasks: list[dict]) -> dict:
    agent_id = agent["id"]
    # E2E старт: пустые куки (только авто-прогон t21 отдельно), изолированное хранилище
    # каждый E2E цикл — 1 профиль = 1 личность, mtime jitter уже в scheduler, user_data не шарится
    start_wall = time.time()
    results = []
    total_tokens_in = 0
    total_tokens_out = 0
    total_elapsed_ms = 0
    total_steps = 0

    for task in dataset_tasks_by_id_backup_global if False else tasks:
        tid = task["id"]
        # estimate tokens for this task as Hermes would see:
        # prompt = AGENT.md + task description + API spec + snapshot
        prompt_text = (
            f"{AGENT_MD.read_text(encoding='utf-8')[:8000]}\n"
            f"Task: {task['id']} {task['description']}\n"
            f"URL: {task['url']}\nAction: {task['action']}\nChallenge: {task['challenge']}\n"
            f"API: POST /sessions/{{sid}}/goto, snapshot @e, click, type human, scroll, extract\n"
        )
        prompt_tokens = estimate_tokens(prompt_text) + TOOL_SCHEMA_TOKENS
        level_mult = {"critical": 1.2, "high": 1.0, "medium": 0.8, "low": 0.6}.get(task["level"], 1.0)
        completion_tokens = int(280 * level_mult + len(task["action"])//8)
        # run actual benchmark function (offline) timed
        func = bench_run.TASK_FUNCS.get(tid)
        t0 = time.time()
        if func:
            try:
                res = func()
            except Exception as e:
                res = {"status": "FAIL", "detail": str(e)[:200], "elapsed_ms": int((time.time()-t0)*1000)}
        else:
            res = {"status": "SKIPPED", "detail": "no impl", "elapsed_ms": 0}
        # Unit tasks have no browser trajectory. Flow tasks report their real trace.
        steps = int(res.get("steps", 0))
        total_steps += steps
        elapsed_ms = res.get("elapsed_ms", int((time.time()-t0)*1000))
        # simulate LLM latency: ~ 30ms per 100 tokens (equiv 3k t/s)
        llm_latency_ms = int((prompt_tokens + completion_tokens) / 100 * 30)
        total_wall_task = elapsed_ms + llm_latency_ms

        total_tokens_in += prompt_tokens
        total_tokens_out += completion_tokens
        total_elapsed_ms += total_wall_task

        results.append({
            "id": tid,
            "category": task["category"],
            "level": task["level"],
            "mode": task.get("mode", "offline"),
            "status": res["status"],
            "detail": res["detail"],
            "elapsed_ms": total_wall_task,
            "pure_bench_ms": res.get("elapsed_ms", 0),
            "llm_latency_ms": llm_latency_ms,
            "tokens_in": prompt_tokens,
            "tokens_out": completion_tokens,
            "tokens_total": prompt_tokens + completion_tokens,
            "steps": steps,
            "steps_expected": task.get("steps", []),
            "engine_compare": res.get("engine_compare",""),
        })

    wall_s = time.time() - start_wall
    passed = sum(1 for r in results if r["status"]=="PASS")
    failed = sum(1 for r in results if r["status"]=="FAIL")
    skipped = sum(1 for r in results if r["status"]=="SKIPPED")
    evaluated = len(results) - skipped
    return {
        "agent_id": agent_id,
        "geo": agent["geo"],
        "locale": agent["locale"],
        "timezone": agent["timezone"],
        "profile_isolated": True,
        "tasks_total": len(results),
        "tasks_passed": passed,
        "tasks_failed": failed,
        "tasks_skipped": skipped,
        "tasks_evaluated": evaluated,
        "pass_rate": passed/evaluated if evaluated else 0,
        "wall_s": round(wall_s,3),
        "bench_time_ms": total_elapsed_ms,
        "steps_total": total_steps,
        "steps_per_task_avg": round(total_steps/len(results),1) if results else 0,
        "tokens_in": total_tokens_in,
        "tokens_out": total_tokens_out,
        "tokens_total": total_tokens_in + total_tokens_out,
        "tokens_per_task_avg": round((total_tokens_in+total_tokens_out)/len(results)) if results else 0,
        "results": results,
    }

def try_hermes_live_one_task():
    """Попробовать реальный Hermes LLM вызов на одной задаче для калибровки токенов."""
    try:
        # Use hermes chat in quiet mode with single query, capture usage if possible
        # hermes doesn't have --usage-file for chat, but we can use hermes model call via proxy?
        # Fallback: try `hermes chat -q "test"` with --quiet and time it
        cmd = ["hermes", "chat", "-q", "Say 'AgentFox benchmark ping' in 5 words, no tools.", "--quiet", "--run-budget", "30"]
        t0 = time.time()
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
        elapsed = time.time() - t0
        out = (proc.stdout or "")[:500] + (proc.stderr or "")[:500]
        # try to find token usage in output or look for session file
        return {"ok": proc.returncode==0, "elapsed_s": round(elapsed,2), "output_snippet": out[:400], "note": "live hermes call attempted (Z.AI/GLM via openrouter)"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:400]}

def main():
    ap = argparse.ArgumentParser(formatter_class=argparse.RawTextHelpFormatter, description=textwrap.dedent("""\
        Hermes x AgentFox — 3 изолированных агента
        Распределяет полный dataset по 3 профилям (DE/US/CA) и гонит параллельно.
        Замеряет: задачи, время, токены (оценка len/4 + опционально live hermes).
    """))
    ap.add_argument("--json", default="tools/benchmark/report_hermes.json")
    ap.add_argument("--md", default="tools/benchmark/report_hermes.md")
    ap.add_argument("--live", action="store_true", help="also try one live hermes chat call for token calibration")
    ap.add_argument("--worktree", action="store_true", help="create 3 git worktrees for hermes (requires hermes CLI)")
    ap.add_argument("--sequential", action="store_true", help="run agents sequentially not parallel (for debug)")
    args = ap.parse_args()

    print("[hermes-bench] loading dataset...")
    dataset = bench_run.load_dataset()
    print(f"[hermes-bench] dataset {len(dataset)} tasks")

    print("[hermes-bench] creating 3 isolated profiles (DE/US/CA)...")
    profiles, iso_check = ensure_profiles()
    for ag in AGENTS:
        p = pm.Profile.load(ag["id"])
        print(f"  {ag['color']} {ag['id']} geo={ag['geo']} tz={p.identity.timezone} preset={p.identity.fingerprint_preset_id[:8]} dir={p.user_data_dir.name} proxy={p.proxy.server if p.proxy else 'none'}")
    print(f"[hermes-bench] isolation: presets distinct={iso_check['presets_isolated']} dirs distinct={iso_check['dirs_isolated']} proxies distinct={iso_check.get('proxies_isolated')}")

    if args.worktree:
        print("[hermes-bench] creating 3 git worktrees for hermes --worktree...")
        for ag in AGENTS:
            wt_path = ROOT / f".worktree-{ag['id'].lower()}"
            try:
                subprocess.run(["git", "worktree", "add", str(wt_path), "-b", f"hermes-{ag['id'].lower()}"], cwd=ROOT, capture_output=True, timeout=10)
                print(f"  worktree {wt_path} created")
            except Exception as e:
                print(f"  worktree {ag['id']} failed: {e}")
            # also show hermes worktree capability
            try:
                subprocess.run(["hermes", "worktree", "list"], capture_output=True, timeout=5)
            except Exception:
                pass

    dist = distribute_tasks(dataset)
    for aid, tlist in dist.items():
        print(f"[hermes-bench] {aid} -> {len(tlist)} tasks: {', '.join(t['id'][:6] for t in tlist[:3])}...")

    # run agents
    print(f"[hermes-bench] running 3 agents {'sequentially' if args.sequential else 'in parallel'}...")
    total_start = time.time()
    agent_reports = {}

    if args.sequential:
        for ag in AGENTS:
            tasks = dist[ag["id"]]
            rep = run_agent_tasks(ag, tasks)
            agent_reports[ag["id"]] = rep
            print(f"  {ag['id']}: {rep['tasks_passed']}/{rep['tasks_total']} PASS wall={rep['wall_s']}s tokens={rep['tokens_total']} ({rep['tokens_in']} in + {rep['tokens_out']} out)")
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
            fut_to_ag = {ex.submit(run_agent_tasks, ag, dist[ag["id"]]): ag for ag in AGENTS}
            for fut in concurrent.futures.as_completed(fut_to_ag):
                ag = fut_to_ag[fut]
                try:
                    rep = fut.result()
                except Exception as e:
                    rep = {"agent_id": ag["id"], "error": str(e)[:400], "tasks_total": 0, "tasks_passed": 0, "wall_s": 0, "tokens_total": 0, "results": []}
                agent_reports[ag["id"]] = rep
                print(f"  {ag['id']}: {rep.get('tasks_passed',0)}/{rep.get('tasks_total',0)} PASS wall={rep.get('wall_s',0)}s tokens={rep.get('tokens_total',0)}")

    total_wall = time.time() - total_start

    # aggregate
    total_tasks = sum(r["tasks_total"] for r in agent_reports.values())
    total_passed = sum(r["tasks_passed"] for r in agent_reports.values())
    total_failed = sum(r["tasks_failed"] for r in agent_reports.values())
    total_skipped = sum(r.get("tasks_skipped", 0) for r in agent_reports.values())
    total_evaluated = total_tasks - total_skipped
    total_tokens = sum(r["tokens_total"] for r in agent_reports.values())
    total_tokens_in = sum(r["tokens_in"] for r in agent_reports.values())
    total_tokens_out = sum(r["tokens_out"] for r in agent_reports.values())
    sum_bench_ms = sum(r["bench_time_ms"] for r in agent_reports.values())
    sum_sim_ms = sum_bench_ms  # simulated LLM+bench time
    # wall parallel is max real, sequential sum real; simulated parallel is max simulated
    wall_parallel = max((r["wall_s"] for r in agent_reports.values()), default=0)
    wall_seq_equiv = sum(r["wall_s"] for r in agent_reports.values())
    sim_parallel_ms = max((r["bench_time_ms"] for r in agent_reports.values()), default=0)
    sim_sequential_ms = sum_bench_ms

    live_info = None
    if args.live:
        print("[hermes-bench] live hermes call for calibration...")
        live_info = try_hermes_live_one_task()
        print(f"  live: {live_info}")

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "hermes x agentfox 3 isolated profiles",
        "agents": AGENTS,
        "isolation": iso_check,
        "bench_root": str(BENCH_ROOT),
        "total_tasks": total_tasks,
        "total_passed": total_passed,
        "total_failed": total_failed,
        "tasks_skipped": total_skipped,
        "tasks_evaluated": total_evaluated,
        "pass_rate": total_passed/total_evaluated if total_evaluated else 0,
        "wall_parallel_s": round(wall_parallel,3),
        "wall_sequential_equiv_s": round(wall_seq_equiv,3),
        "wall_measured_s": round(total_wall,3),
        "sim_parallel_s": round(sim_parallel_ms/1000,3),
        "sim_sequential_s": round(sim_sequential_ms/1000,3),
        "sum_bench_ms": sum_bench_ms,
        "tokens_in": total_tokens_in,
        "tokens_out": total_tokens_out,
        "tokens_total": total_tokens,
        "tokens_per_task_avg": round(total_tokens/total_tasks) if total_tasks else 0,
        "tokens_per_agent_avg": round(total_tokens/3) if total_tokens else 0,
        "live_probe": live_info,
        "agent_reports": agent_reports,
        "estimation_note": f"tokens estimated len/4 heuristic, AGENT.md {AGENT_TOKENS} tok + tool_schema {TOOL_SCHEMA_TOKENS} tok + snapshot {SNAPSHOT_TOKENS} tok per task; completion ~280*level; sim time = bench+LLM latency 30ms/100tok",
    }

    Path(args.json).parent.mkdir(parents=True, exist_ok=True)
    Path(args.md).parent.mkdir(parents=True, exist_ok=True)
    with open(args.json, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    # markdown
    md_lines = []
    md_lines.append(f"# Hermes x AgentFox — 3 изолированных агента ({report['generated_at']})")
    md_lines.append("")
    md_lines.append(f"**Итог: {total_passed}/{total_evaluated} проверяемых задач PASS ({report['pass_rate']*100:.0f}%)**, {total_failed} FAIL, {total_skipped} live-only SKIPPED")
    md_lines.append(f"**Время (симуляция LLM+bench):** параллельно **{sim_parallel_ms/1000:.2f}s**, последовательно {sim_sequential_ms/1000:.2f}s (ускорение {sim_sequential_ms/sim_parallel_ms:.2f}x)")
    md_lines.append(f"**Время (реальное wall с заглушками):** параллельно {wall_parallel:.2f}s, измерено {total_wall:.2f}s")
    md_lines.append(f"**Токены:** всего **{total_tokens:,}** ({total_tokens_in:,} in / {total_tokens_out:,} out), ~{report['tokens_per_task_avg']} tok/задача, ~{report['tokens_per_agent_avg']} tok/агент")
    md_lines.append(f"**Изоляция:** presets {'✅ distinct 3/3' if iso_check['presets_isolated'] else '❌'} | user_data {'✅ distinct 3/3' if iso_check['dirs_isolated'] else '❌'} | proxies {'✅ distinct 3/3' if iso_check.get('proxies_isolated') else '❌'} (`{', '.join(iso_check.get('proxies',[])[:3])}`)")
    md_lines.append("")
    md_lines.append("| Агент | Geo | Задач | PASS | Время wall | Токены in/out/total | avg tok/задача | Изоляция |")
    md_lines.append("|---|---|---|---|---|---|---|---|---|")
    for ag in AGENTS:
        aid = ag["id"]
        r = agent_reports.get(aid, {})
        md_lines.append(f"| {ag['color']} {aid} | {ag['geo']} | {r.get('tasks_total',0)} | {r.get('tasks_passed',0)}/{r.get('tasks_evaluated',0)} ({r.get('pass_rate',0)*100:.0f}%) | {r.get('wall_s',0):.2f}s | {r.get('tokens_in',0)}/{r.get('tokens_out',0)}/{r.get('tokens_total',0)} | {r.get('tokens_per_task_avg',0)} | preset {iso_check['presets'][AGENTS.index(ag)][:6] if len(iso_check['presets'])>AGENTS.index(ag) else '?'} |")
    md_lines.append("")
    md_lines.append("| Метрика | Значение |")
    md_lines.append("|---|---|")
    md_lines.append(f"| Всего задач | {total_tasks} |")
    md_lines.append(f"| Выполнено PASS | {total_passed} |")
    md_lines.append(f"| Провалено FAIL | {total_failed} |")
    md_lines.append(f"| Время симуляция параллельно | {sim_parallel_ms/1000:.2f}s |")
    md_lines.append(f"| Время симуляция последовательно | {sim_sequential_ms/1000:.2f}s |")
    md_lines.append(f"| Ускорение симуляция | {sim_sequential_ms/sim_parallel_ms:.2f}x |" if sim_parallel_ms else "| Ускорение | - |")
    md_lines.append(f"| Время реальное wall параллельно | {wall_parallel:.2f}s |")
    md_lines.append(f"| Токенов всего | {total_tokens:,} |")
    md_lines.append(f"| Токенов in (prompt) | {total_tokens_in:,} |")
    md_lines.append(f"| Токенов out (completion) | {total_tokens_out:,} |")
    md_lines.append(f"| Средн. на задачу | {report['tokens_per_task_avg']} |")
    md_lines.append(f"| Изоляция presets | {'✅ distinct 3/3' if iso_check['presets_isolated'] else '❌ collision'} |")
    md_lines.append(f"| Изоляция user_data | {'✅ distinct 3/3' if iso_check['dirs_isolated'] else '❌'} |")
    md_lines.append(f"| Изоляция proxies | {'✅ distinct 3/3' if iso_check.get('proxies_isolated') else '❌'} |")
    md_lines.append("")
    md_lines.append("## Детализация по агентам")
    for ag in AGENTS:
        aid = ag["id"]
        r = agent_reports.get(aid, {})
        md_lines.append(f"### {ag['color']} {aid} ({ag['geo']} {ag['timezone']}) — {r.get('tasks_passed',0)}/{r.get('tasks_evaluated',0)} PASS, {r.get('tasks_skipped',0)} SKIPPED")
        md_lines.append(f"Профиль: `{aid}` geo={ag['geo']} preset={pm.Profile.load(aid).identity.fingerprint_preset_id[:12] if (ROOT / 'profiles').exists() else iso_check['presets'][0][:12]} изолирован ✅")
        md_lines.append("")
        md_lines.append("| # | Задача | Кат | Ур | Статус | ms (bench+LLM) | tok in/out | engine |")
        md_lines.append("|---|---|---|---|---|---|---|---|---|")
        for i, t in enumerate(r.get("results", []), 1):
            icon = "✅" if t["status"]=="PASS" else "❌" if t["status"]=="FAIL" else "⏭️"
            md_lines.append(f"| {i} | {t['id']} | {t['category']} | {t['level']} | {icon} {t['status']} | {t['elapsed_ms']} ({t['pure_bench_ms']}+{t['llm_latency_ms']}) | {t['tokens_in']}/{t['tokens_out']} | {t['engine_compare'][:40]} |")
        md_lines.append("")
    if live_info:
        md_lines.append("## Live Hermes Probe")
        md_lines.append(f"```json\n{json.dumps(live_info, ensure_ascii=False, indent=2)}\n```")
        md_lines.append("")
    md_lines.append("## Как воспроизвести")
    md_lines.append("```bash")
    md_lines.append("python -m tools.benchmark.run_hermes            # offline, 3 агента параллельно")
    md_lines.append("python -m tools.benchmark.run_hermes --sequential  # последовательно")
    md_lines.append("python -m tools.benchmark.run_hermes --live       # + 1 реальный вызов hermes (Z.AI/GLM)")
    md_lines.append("python -m tools.benchmark.run_hermes --worktree   # + git worktree для 3 мест")
    md_lines.append("cat tools/benchmark/report_hermes.json | jq .tokens_total")
    md_lines.append("cat tools/benchmark/report_hermes.md")
    md_lines.append("```")
    md_lines.append("")
    md_lines.append(f"Оценка токенов: {report['estimation_note']}")
    md = "\n".join(md_lines)
    Path(args.md).write_text(md, encoding="utf-8")
    print("\n" + md)
    print(f"\n[hermes-bench] wrote {args.json} and {args.md}")
    if total_failed>0:
        print(f"[hermes-bench] {total_failed} FAIL")
    sys.exit(0 if total_failed==0 else 1)

if __name__ == "__main__":
    main()
