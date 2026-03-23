#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

TASKS_1_5 = [
    "task_01_rollout_plan",
    "task_02_incident_triage",
    "task_03_health_snapshot",
    "task_04_delegate_workflow",
    "task_05_route_promotion_exec",
]
EXECUTION_TASKS = ["task_06_delegate_api_exec", "task_07_discord_dryrun"]
LIVE_EXTERNAL_TASKS = ["task_08_discord_live_smoke"]


def _run(cmd: list[str], cwd: Path) -> dict[str, Any]:
    proc = subprocess.run(cmd, cwd=str(cwd), text=True, capture_output=True)
    return {
        "cmd": cmd,
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


def _augment_failure_details(item: dict[str, Any]) -> dict[str, Any]:
    report = item.get("report")
    if not isinstance(report, dict):
        return item
    failure_reason = str(report.get("failure_reason", "")).strip()
    failed_checks = report.get("failed_checks")
    if failure_reason:
        item["failure_reason"] = failure_reason
    if isinstance(failed_checks, list) and failed_checks:
        item["failed_checks"] = [str(x) for x in failed_checks]
    daemon_status = report.get("daemon_health_status")
    if isinstance(daemon_status, int):
        item["daemon_health_status"] = daemon_status
    return item


def _task_paths(task: str) -> tuple[Path, Path]:
    base = ROOT / "evaluation" / "agent_tasks" / task
    return base / "job.jsonl", base / "results.jsonl"


def _check_task(task: str) -> dict[str, Any]:
    _, results = _task_paths(task)
    cmd = [
        "python3",
        "scripts/agent_task_check.py",
        "--task",
        task,
        "--results",
        str(results.relative_to(ROOT)),
    ]
    res = _run(cmd, ROOT)
    parsed: dict[str, Any] | None = None
    try:
        parsed = json.loads(res["stdout"] or "{}")
    except json.JSONDecodeError:
        parsed = None
    return {
        "task": task,
        "type": "content",
        "check_returncode": res["returncode"],
        "passed": bool(parsed and parsed.get("passed", False) and res["returncode"] == 0),
        "report": parsed,
        "check_cmd": cmd,
        "check_stderr": res["stderr"],
    }


def _run_task(task: str, config_path: str) -> dict[str, Any]:
    job, results = _task_paths(task)
    cmd = [
        "python3",
        "-m",
        "mmctl",
        "batch",
        "run",
        str(job.relative_to(ROOT)),
        "--output-file",
        str(results.relative_to(ROOT)),
        "--parallel",
        "1",
        "--config",
        config_path,
    ]
    return _run(cmd, ROOT)


def _run_task6(output_path: Path) -> dict[str, Any]:
    cmd = [
        "python3",
        "evaluation/agent_tasks/task_06_delegate_api_exec/run.py",
        "--output",
        str(output_path.relative_to(ROOT)),
    ]
    return _run(cmd, ROOT)


def _check_task6(output_path: Path) -> dict[str, Any]:
    cmd = [
        "python3",
        "evaluation/agent_tasks/task_06_delegate_api_exec/check.py",
        "--results",
        str(output_path.relative_to(ROOT)),
    ]
    res = _run(cmd, ROOT)
    parsed: dict[str, Any] | None = None
    try:
        parsed = json.loads(res["stdout"] or "{}")
    except json.JSONDecodeError:
        parsed = None
    return _augment_failure_details({
        "task": "task_06_delegate_api_exec",
        "type": "execution",
        "check_returncode": res["returncode"],
        "passed": bool(parsed and parsed.get("passed", False) and res["returncode"] == 0),
        "report": parsed,
        "check_cmd": cmd,
        "check_stderr": res["stderr"],
    })


def _run_task7(output_path: Path) -> dict[str, Any]:
    cmd = [
        "python3",
        "evaluation/agent_tasks/task_07_discord_dryrun/run.py",
        "--output",
        str(output_path.relative_to(ROOT)),
    ]
    return _run(cmd, ROOT)


def _check_task7(output_path: Path) -> dict[str, Any]:
    cmd = [
        "python3",
        "evaluation/agent_tasks/task_07_discord_dryrun/check.py",
        "--results",
        str(output_path.relative_to(ROOT)),
    ]
    res = _run(cmd, ROOT)
    parsed: dict[str, Any] | None = None
    try:
        parsed = json.loads(res["stdout"] or "{}")
    except json.JSONDecodeError:
        parsed = None
    return _augment_failure_details({
        "task": "task_07_discord_dryrun",
        "type": "execution",
        "check_returncode": res["returncode"],
        "passed": bool(parsed and parsed.get("passed", False) and res["returncode"] == 0),
        "report": parsed,
        "check_cmd": cmd,
        "check_stderr": res["stderr"],
    })


def _run_task8(output_path: Path) -> dict[str, Any]:
    cmd = [
        "python3",
        "evaluation/agent_tasks/task_08_discord_live_smoke/run.py",
        "--output",
        str(output_path.relative_to(ROOT)),
    ]
    return _run(cmd, ROOT)


def _check_task8(output_path: Path) -> dict[str, Any]:
    cmd = [
        "python3",
        "evaluation/agent_tasks/task_08_discord_live_smoke/check.py",
        "--results",
        str(output_path.relative_to(ROOT)),
    ]
    res = _run(cmd, ROOT)
    parsed: dict[str, Any] | None = None
    try:
        parsed = json.loads(res["stdout"] or "{}")
    except json.JSONDecodeError:
        parsed = None
    return _augment_failure_details({
        "task": "task_08_discord_live_smoke",
        "type": "execution",
        "check_returncode": res["returncode"],
        "passed": bool(parsed and parsed.get("passed", False) and res["returncode"] == 0),
        "report": parsed,
        "check_cmd": cmd,
        "check_stderr": res["stderr"],
    })


def main() -> int:
    parser = argparse.ArgumentParser(description="Run/check internal agent task suite (tasks 01-07, optional live task 08).")
    parser.add_argument("--run", action="store_true", help="Run tasks before checking results.")
    parser.add_argument("--config", default="config/config.example.yaml", help="Config path for mmctl batch run.")
    parser.add_argument(
        "--tasks",
        default="all",
        help="Comma-separated tasks (e.g. task_01_rollout_plan,task_06_delegate_api_exec) or 'all'.",
    )
    parser.add_argument(
        "--json-out",
        default="",
        help="Write suite summary JSON to this path. Use 'auto' for timestamped report in evaluation/agent_tasks/suite_reports/.",
    )
    parser.add_argument(
        "--include-live-external",
        action="store_true",
        help="Include optional live external task(s), currently task_08_discord_live_smoke.",
    )
    args = parser.parse_args()

    if args.tasks.strip().lower() == "all":
        requested = TASKS_1_5 + EXECUTION_TASKS
        if args.include_live_external:
            requested.extend(LIVE_EXTERNAL_TASKS)
    else:
        requested = [item.strip() for item in args.tasks.split(",") if item.strip()]

    suite_results: list[dict[str, Any]] = []
    run_steps: list[dict[str, Any]] = []

    for task in requested:
        if task in TASKS_1_5:
            if args.run:
                run_res = _run_task(task, args.config)
                run_steps.append({"task": task, "run": run_res})
            suite_results.append(_check_task(task))
            continue

        if task == "task_06_delegate_api_exec":
            out = ROOT / "evaluation" / "agent_tasks" / "task_06_delegate_api_exec" / "results.json"
            if args.run:
                run_res = _run_task6(out)
                run_steps.append({"task": task, "run": run_res})
            suite_results.append(_check_task6(out))
            continue

        if task == "task_07_discord_dryrun":
            out = ROOT / "evaluation" / "agent_tasks" / "task_07_discord_dryrun" / "results.json"
            if args.run:
                run_res = _run_task7(out)
                run_steps.append({"task": task, "run": run_res})
            suite_results.append(_check_task7(out))
            continue

        if task == "task_08_discord_live_smoke":
            out = ROOT / "evaluation" / "agent_tasks" / "task_08_discord_live_smoke" / "results.json"
            if args.run:
                run_res = _run_task8(out)
                run_steps.append({"task": task, "run": run_res})
            suite_results.append(_check_task8(out))
            continue

        suite_results.append(
            {
                "task": task,
                "type": "unknown",
                "passed": False,
                "error": "unknown task",
            }
        )

    passed = all(bool(item.get("passed", False)) for item in suite_results)
    summary = {
        "passed": passed,
        "total": len(suite_results),
        "passed_count": sum(1 for item in suite_results if item.get("passed", False)),
        "failed_count": sum(1 for item in suite_results if not item.get("passed", False)),
        "results": suite_results,
    }
    if not passed:
        failure_summary: list[dict[str, Any]] = []
        for item in suite_results:
            if item.get("passed", False):
                continue
            entry: dict[str, Any] = {
                "task": str(item.get("task", "")),
                "check_returncode": item.get("check_returncode"),
            }
            if item.get("failure_reason"):
                entry["failure_reason"] = item.get("failure_reason")
            if item.get("failed_checks"):
                entry["failed_checks"] = item.get("failed_checks")
            if isinstance(item.get("daemon_health_status"), int):
                entry["daemon_health_status"] = item.get("daemon_health_status")
            failure_summary.append(entry)
        if failure_summary:
            summary["failure_summary"] = failure_summary
    if args.run:
        summary["run_steps"] = run_steps

    json_out = (args.json_out or "").strip()
    if json_out:
        if json_out.lower() == "auto":
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            out_path = ROOT / "evaluation" / "agent_tasks" / "suite_reports" / f"suite_{stamp}.json"
        else:
            out_path = (ROOT / Path(json_out)).resolve() if not Path(json_out).is_absolute() else Path(json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        summary["json_out"] = str(out_path)

    print(json.dumps(summary, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
