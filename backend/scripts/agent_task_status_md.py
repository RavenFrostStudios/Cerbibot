#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _as_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate markdown status from agent task suite JSON.")
    parser.add_argument(
        "--input",
        default="evaluation/agent_tasks/suite_reports/latest.json",
        help="Path to suite JSON report.",
    )
    parser.add_argument(
        "--output",
        default="evaluation/agent_tasks/suite_reports/latest_status.md",
        help="Path to markdown status output.",
    )
    args = parser.parse_args()

    in_path = Path(args.input)
    if not in_path.exists():
        raise SystemExit(f"input report not found: {in_path}")

    payload = json.loads(in_path.read_text(encoding="utf-8"))
    results = list(payload.get("results") or [])
    passed = bool(payload.get("passed", False))

    content_rows: list[dict[str, Any]] = [
        item for item in results if str(item.get("type", "")) == "content" and isinstance(item.get("report"), dict)
    ]
    execution_rows: list[dict[str, Any]] = [
        item for item in results if str(item.get("type", "")) == "execution" and isinstance(item.get("report"), dict)
    ]

    costs = [_as_float((item.get("report") or {}).get("cost")) for item in content_rows]
    costs = [c for c in costs if c is not None]
    tokens = [_as_int((item.get("report") or {}).get("tokens")) for item in content_rows]
    tokens = [t for t in tokens if t is not None]

    avg_cost = (sum(costs) / len(costs)) if costs else None
    avg_tokens = (sum(tokens) / len(tokens)) if tokens else None

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    status_emoji = "PASS" if passed else "FAIL"

    lines: list[str] = []
    lines.append("# Agent Task Suite Status")
    lines.append("")
    lines.append(f"- Generated: {generated_at}")
    lines.append(f"- Overall: **{status_emoji}**")
    lines.append(f"- Passed: {int(payload.get('passed_count', 0))}/{int(payload.get('total', 0))}")
    if avg_cost is not None:
        lines.append(f"- Avg content task cost: `${avg_cost:.6f}`")
    if avg_tokens is not None:
        lines.append(f"- Avg content task tokens: `{int(round(avg_tokens))}`")
    lines.append("")

    lines.append("## Task Results")
    lines.append("")
    lines.append("| Task | Type | Status | Key |")
    lines.append("|---|---|---|---|")

    for item in results:
        task = str(item.get("task", ""))
        task_type = str(item.get("type", ""))
        ok = bool(item.get("passed", False))
        icon = "PASS" if ok else "FAIL"

        report = item.get("report") or {}
        key = ""
        if task_type == "content":
            matched = report.get("matched_signals")
            required = report.get("required_signals")
            cost = report.get("cost")
            tokens_val = report.get("tokens")
            parts = []
            if matched is not None and required is not None:
                parts.append(f"signals {matched}/{required}")
            if cost is not None:
                try:
                    parts.append(f"cost ${float(cost):.6f}")
                except (TypeError, ValueError):
                    pass
            if tokens_val is not None:
                parts.append(f"tokens {tokens_val}")
            key = ", ".join(parts)
        elif task_type == "execution":
            job_id = str(report.get("job_id", ""))
            status = str(report.get("status", ""))
            key = f"job {job_id}, status {status}" if job_id else f"status {status}"

        lines.append(f"| `{task}` | `{task_type}` | **{icon}** | {key} |")

    lines.append("")
    lines.append("## Failing Tasks")
    lines.append("")

    failed = [item for item in results if not bool(item.get("passed", False))]
    if not failed:
        lines.append("None")
    else:
        for item in failed:
            task = str(item.get("task", ""))
            report = item.get("report") or {}
            reasons = report.get("reasons") if isinstance(report, dict) else None
            if isinstance(reasons, list) and reasons:
                lines.append(f"- `{task}`: {', '.join(str(r) for r in reasons)}")
            else:
                lines.append(f"- `{task}`: failed")

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(
        json.dumps(
            {
                "ok": True,
                "input": str(in_path),
                "output": str(out_path),
                "overall_passed": passed,
                "total": int(payload.get("total", 0)),
                "passed": int(payload.get("passed_count", 0)),
                "failed": int(payload.get("failed_count", 0)),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
