#!/usr/bin/env python3
"""Report what is and is not measured by the engine comparison.

This command executes AgentFox's honest offline benchmark. It deliberately does
not invent PASS/FAIL values for vanilla Playwright or raw Camoufox: those
engines require a separate live run with the same targets and proxies.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

DATASET = Path(__file__).resolve().parent / "dataset.json"


def load_dataset() -> list[dict]:
    return json.loads(DATASET.read_text(encoding="utf-8"))


def _not_measured(engine: str, task: dict) -> dict:
    return {
        "status": "NOT_MEASURED",
        "detail": f"{engine} was not executed; run an authorized live comparison",
        "engine": engine,
        "task_id": task["id"],
        "elapsed_ms": 0,
    }


def engine_vanilla(task: dict) -> dict:
    return _not_measured("vanilla_playwright", task)


def engine_camoufox_raw(task: dict) -> dict:
    return _not_measured("camoufox_raw", task)


def engine_agentfox(task: dict) -> dict:
    import tools.benchmark.run as benchmark

    started = time.monotonic()
    function = benchmark.TASK_FUNCS.get(task["id"])
    if not function:
        return {"status": "SKIPPED", "detail": "no implementation", "engine": "agentfox", "elapsed_ms": 0}
    try:
        result = function()
    except Exception as exc:
        result = {"status": "FAIL", "detail": str(exc)[:300], "engine": "agentfox"}
    result.setdefault("elapsed_ms", int((time.monotonic() - started) * 1000))
    result["engine"] = "agentfox"
    return result


ENGINES = {
    "vanilla_playwright": engine_vanilla,
    "camoufox_raw": engine_camoufox_raw,
    "agentfox": engine_agentfox,
}


def resolve_proxies(proxy_file: str | None) -> dict:
    """Load proxy URLs without printing credentials."""
    if proxy_file:
        try:
            data = json.loads(Path(proxy_file).read_text(encoding="utf-8"))
            if isinstance(data, dict) and "proxies" in data:
                return {item.get("geo", "?"): item.get("server", "") for item in data["proxies"]}
            return data
        except Exception:
            return {}
    env_map = {"DE": "AGENTFOX_PROXY_DE", "US": "AGENTFOX_PROXY_US", "RU": "AGENTFOX_PROXY_RU", "GB": "AGENTFOX_PROXY_GB"}
    return {geo: os.environ[name] for geo, name in env_map.items() if os.environ.get(name)}


def _summary(results: list[dict]) -> dict:
    measured = [result for result in results if result["status"] != "NOT_MEASURED"]
    evaluated = [result for result in measured if result["status"] != "SKIPPED"]
    passed = sum(result["status"] == "PASS" for result in evaluated)
    return {
        "passed": passed,
        "evaluated": len(evaluated),
        "skipped": sum(result["status"] == "SKIPPED" for result in measured),
        "not_measured": sum(result["status"] == "NOT_MEASURED" for result in results),
        "rate": passed / len(evaluated) if evaluated else None,
        "ms": sum(result.get("elapsed_ms", 0) for result in results),
    }


def format_markdown(report: dict) -> str:
    lines = [
        f"# Engine comparison boundary ({report['generated_at']})",
        "",
        "This is a measurement boundary, not a prediction table. Only AgentFox is executed offline; vanilla Playwright and raw Camoufox are NOT_MEASURED until the same live target matrix is run.",
        "",
        "| Engine | PASS | Evaluated | SKIPPED | NOT_MEASURED | Rate |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name, summary in report["totals"].items():
        rate = "n/a" if summary["rate"] is None else f"{summary['rate'] * 100:.0f}%"
        lines.append(f"| {name} | {summary['passed']} | {summary['evaluated']} | {summary['skipped']} | {summary['not_measured']} | {rate} |")
    lines.extend(["", "| Task | Vanilla | Raw Camoufox | AgentFox |", "|---|---|---|---|"])
    for task in report["dataset"]:
        row = [task["id"]]
        for engine in ENGINES:
            result = next(item for item in report["results"][engine] if item["task_id"] == task["id"])
            row.append(result["status"])
        lines.append("| " + " | ".join(row) + " |")
    lines.extend([
        "",
        "## Reproduce",
        "",
        "```bash",
        "python3 -m tools.benchmark.compare_engines",
        "python3 -m tools.benchmark.live_run --tasks t01,t04,t06 --proxy-index 0",
        "```",
    ])
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Measured AgentFox run plus explicit engine comparison gaps")
    parser.add_argument("--proxy-file", default=None, help="optional proxy JSON for a separate live check")
    parser.add_argument("--live", action="store_true", help="check supplied proxy endpoints; does not fake engine task results")
    parser.add_argument("--json", dest="json_out", default="tools/benchmark/compare.json")
    parser.add_argument("--md", dest="md_out", default="tools/benchmark/compare.md")
    args = parser.parse_args()

    dataset = load_dataset()
    proxies = resolve_proxies(args.proxy_file)
    if args.live and proxies:
        from core.proxy_pool import ProxyConfig, check_proxy

        for geo, url in proxies.items():
            if not url or "example" in url:
                continue
            try:
                check_proxy(ProxyConfig(server=url, provider="generic", geo=geo), timeout=5)
            except Exception:
                pass

    results = {engine: [] for engine in ENGINES}
    for engine, function in ENGINES.items():
        for task in dataset:
            result = function(task)
            results[engine].append({**result, "task_id": task["id"], "category": task["category"], "level": task["level"]})

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset_size": len(dataset),
        "dataset": dataset,
        "proxies_configured": bool(proxies),
        "live_requested": args.live,
        "totals": {engine: _summary(items) for engine, items in results.items()},
        "results": results,
    }
    json_path = Path(args.json_out)
    md_path = Path(args.md_out)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown = format_markdown(report)
    md_path.write_text(markdown, encoding="utf-8")
    print(markdown)
    print(f"\n[compare] wrote {json_path} and {md_path}")


if __name__ == "__main__":
    main()
