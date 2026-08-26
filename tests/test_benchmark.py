import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_benchmark_dataset_has_explicit_modes_and_unique_ids():
    dataset = json.loads((ROOT / "tools/benchmark/dataset.json").read_text(encoding="utf-8"))
    ids = [task["id"] for task in dataset]

    assert len(dataset) == 40
    assert len(ids) == len(set(ids))
    assert {task["mode"] for task in dataset} == {"offline", "live"}
    assert sum(task["mode"] == "offline" for task in dataset) == 28
    assert sum(task["mode"] == "live" for task in dataset) == 12
    assert all(task.get("steps") for task in dataset if task["id"].startswith("t5"))


def test_offline_benchmark_executes_real_checks_without_live_tasks():
    from tools.benchmark.run import run_dataset

    report = run_dataset()

    assert report["total"] == 40
    assert report["evaluated"] == 28
    assert report["passed"] == 28
    assert report["failed"] == 0
    assert report["skipped"] == 12
    assert report["pass_rate"] == 1
    assert all(
        result["steps"] == len(result["steps_expected"])
        for result in report["results"]
        if result["steps_expected"]
    )


def test_live_only_tasks_never_report_offline_pass():
    from tools.benchmark.run import LIVE_ONLY_TASKS, run_dataset

    report = run_dataset()
    statuses = {result["id"]: result["status"] for result in report["results"]}

    assert all(statuses[task_id] == "SKIPPED" for task_id in LIVE_ONLY_TASKS)
