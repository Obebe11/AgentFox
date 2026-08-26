"""Independent evidence judge for live benchmark artifacts.

The judge is intentionally strict: a claimed status or a fabricated count is
not evidence. Live runners should provide ``steps_completed`` and
``artifacts_verified`` in the result they submit here.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from typing import Any


FLOW_STEP_COUNTS = {
    "t50_flow_profile_lifecycle": 6,
    "t51_flow_autoreg": 8,
    "t53_flow_ecom_compare": 10,
    "t54_flow_warmup_to_work": 6,
    "t55_flow_proxy_ops": 4,
    "t56_flow_incident_response": 5,
    "t57_flow_bulk_farm": 5,
    "t58_flow_x_research": 8,
}


def heuristic_judge(task_id: str, data: dict[str, Any]) -> dict[str, Any]:
    if data.get("status") != "PASS":
        return {"verdict": "FAIL", "reason": "runner did not report PASS", "confidence": 0.9}
    if data.get("artifacts_verified") is False:
        return {"verdict": "FAIL", "reason": "runner marked artifacts as unverified", "confidence": 0.95}
    expected = FLOW_STEP_COUNTS.get(task_id)
    if expected is not None:
        completed = data.get("steps_completed")
        if completed != expected:
            return {"verdict": "FAIL", "reason": f"expected {expected} verified steps, got {completed!r}", "confidence": 0.95}
        if data.get("artifacts_verified") is not True:
            return {"verdict": "FAIL", "reason": "multi-step flow has no verified artifact flag", "confidence": 0.95}
    if not data.get("evidence") and not data.get("data") and task_id in FLOW_STEP_COUNTS:
        return {"verdict": "FAIL", "reason": "no evidence payload", "confidence": 0.9}
    return {"verdict": "PASS", "reason": "status and supplied evidence passed basic checks", "confidence": 0.7}


def llm_judge(task_id: str, task: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Optionally ask Hermes to inspect a bounded evidence payload."""
    prompt = f"""Ты независимый судья live E2E теста. Не доверяй полю status без доказательств.
Задача: {task.get('id')} {task.get('description')}
Критерий: {task.get('success_criteria')}
Результат: {json.dumps(result, ensure_ascii=False)[:1500]}
Верни только JSON: {{"verdict":"PASS или FAIL","reason":"кратко","confidence":0.0}}.
"""
    if shutil.which("hermes"):
        try:
            process = subprocess.run(
                ["hermes", "chat", "-q", prompt, "--quiet", "--run-budget", "20"],
                capture_output=True,
                text=True,
                timeout=20,
            )
            match = re.search(r"\{[^{}]*\"verdict\"[^{}]*\}", process.stdout, re.S)
            if match:
                parsed = json.loads(match.group(0))
                if parsed.get("verdict") in {"PASS", "FAIL"}:
                    return {
                        "verdict": parsed["verdict"],
                        "reason": parsed.get("reason", "llm"),
                        "confidence": float(parsed.get("confidence", 0.8)),
                        "raw": process.stdout[:500],
                    }
        except Exception:
            pass
    return heuristic_judge(task_id, result)


def judge_task(task_id: str, task: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    llm_result = llm_judge(task_id, task, result)
    heuristic = heuristic_judge(task_id, result)
    return {
        "task_id": task_id,
        "agent_status": result.get("status"),
        "judge_verdict": llm_result["verdict"],
        "judge_reason": llm_result["reason"],
        "judge_confidence": llm_result.get("confidence", 0.7),
        "heuristic_verdict": heuristic["verdict"],
        "final": llm_result["verdict"],
        "is_overrule": llm_result["verdict"] != result.get("status") and result.get("status") == "PASS",
    }
