#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import yaml

DEFAULT_DEGRADATION_PATTERNS = (
    "deterministic fallback",
    "single-pass",
    "single pass",
    "provider returned empty response",
    "low-signal",
    "low_signal",
    "empty final answer",
)


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def _contains_any(text: str, needles: list[str]) -> bool:
    lower = text.lower()
    return any(item.lower() in lower for item in needles)


def main() -> int:
    parser = argparse.ArgumentParser(description="Fast rubric checker for agent task outputs.")
    parser.add_argument("--task", required=True, help="Task folder name under evaluation/agent_tasks/")
    parser.add_argument("--results", required=True, help="Path to batch results JSONL")
    parser.add_argument("--base-dir", default="evaluation/agent_tasks")
    parser.add_argument(
        "--require-no-degradation",
        action="store_true",
        help="Fail if guardian flags contain fallback/degradation patterns.",
    )
    parser.add_argument(
        "--degradation-pattern",
        action="append",
        default=[],
        help="Additional case-insensitive substring pattern to treat as degradation. Can be repeated.",
    )
    args = parser.parse_args()

    task_dir = Path(args.base_dir).expanduser() / args.task
    rubric_path = task_dir / "rubric.yaml"
    if not rubric_path.exists():
        raise SystemExit(f"rubric not found: {rubric_path}")

    results_path = Path(args.results).expanduser()
    if not results_path.exists():
        raise SystemExit(f"results not found: {results_path}")

    rubric = yaml.safe_load(rubric_path.read_text(encoding="utf-8")) or {}
    rows = _load_jsonl(results_path)
    if not rows:
        raise SystemExit("no result rows found")

    row = rows[-1]
    status = str(row.get("status", ""))
    answer = str(row.get("answer", "") or "")
    fail_on_error = bool((rubric.get("scoring") or {}).get("fail_on_error_status", True))
    fail_on_empty = bool((rubric.get("scoring") or {}).get("fail_on_empty_answer", True))
    min_matches = int((rubric.get("scoring") or {}).get("min_required_signal_matches", 1))
    require_no_degradation = bool(
        args.require_no_degradation or bool((rubric.get("scoring") or {}).get("require_no_degradation", False))
    )

    checks: list[dict[str, Any]] = []
    required_signals = list(rubric.get("required_signals") or [])
    for signal in required_signals:
        name = str(signal.get("name", "signal"))
        any_of = [str(item) for item in list(signal.get("any_of") or [])]
        matched = _contains_any(answer, any_of)
        checks.append({"name": name, "matched": matched})

    matched_count = sum(1 for item in checks if item["matched"])
    passed = True
    reasons: list[str] = []

    if fail_on_error and status != "ok":
        passed = False
        reasons.append(f"status={status}")
    if fail_on_empty and not answer.strip():
        passed = False
        reasons.append("empty_answer")
    if matched_count < min_matches:
        passed = False
        reasons.append(f"signal_matches={matched_count}/{min_matches}")

    guardian_flags = [str(item) for item in list(row.get("guardian_flags", []) or [])]
    raw_patterns = list(DEFAULT_DEGRADATION_PATTERNS)
    raw_patterns.extend([str(item) for item in list((rubric.get("degradation_patterns") or [])) if str(item).strip()])
    raw_patterns.extend([str(item) for item in args.degradation_pattern if str(item).strip()])
    patterns = list(dict.fromkeys(raw_patterns))

    degradation_hits: list[str] = []
    for flag in guardian_flags:
        low = flag.lower()
        if any(pat.lower() in low for pat in patterns):
            degradation_hits.append(flag)

    if require_no_degradation and degradation_hits:
        passed = False
        reasons.append(f"degradation_hits={len(degradation_hits)}")

    report = {
        "task": args.task,
        "passed": passed,
        "status": status,
        "matched_signals": matched_count,
        "required_signals": min_matches,
        "checks": checks,
        "reasons": reasons,
        "cost": row.get("cost"),
        "tokens": row.get("tokens"),
        "guardian_flags": guardian_flags,
        "require_no_degradation": require_no_degradation,
        "degradation_hits": degradation_hits,
        "degradation_patterns": patterns,
    }
    print(json.dumps(report, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
