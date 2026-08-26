#!/usr/bin/env python3
"""Compatibility report for the honest AgentFox benchmark.

The former version manufactured a failing ``without_agentfox`` result. This
version reuses the measured offline runner and leaves an external baseline
explicitly unmeasured until it is executed on the same targets.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from tools.benchmark.run import format_markdown, run_dataset


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Measured AgentFox report without fabricated baseline failures")
    parser.add_argument("--json", dest="json_out", default="tools/benchmark/honest.json")
    parser.add_argument("--md", dest="md_out", default="tools/benchmark/honest.md")
    args = parser.parse_args()

    measured = run_dataset()
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "honest AgentFox offline run; external baselines not measured",
        "measured": measured,
        "baseline": {
            "vanilla_playwright": "NOT_MEASURED",
            "camoufox_raw": "NOT_MEASURED",
            "reason": "run the same authorized live target matrix before comparing engines",
        },
    }
    json_path = Path(args.json_out)
    md_path = Path(args.md_out)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown = format_markdown(measured)
    markdown = (
        "# Honest AgentFox report\n\n"
        "The old synthetic without-agentfox comparison was removed.\n\n"
        + markdown
        + "\n\nBaseline: `vanilla_playwright` and `camoufox_raw` are `NOT_MEASURED`.\n"
    )
    md_path.write_text(markdown, encoding="utf-8")
    print(markdown)
    print(f"\n[honest] wrote {json_path} and {md_path}")
    raise SystemExit(1 if measured["failed"] else 0)


if __name__ == "__main__":
    main()
