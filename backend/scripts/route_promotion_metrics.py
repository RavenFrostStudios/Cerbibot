#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


LOW_SIGNAL_MARKERS = (
    "low-signal",
    "low_signal",
    "too_short",
    "insufficient_key_points",
    "weak_opposition_engagement",
    "instruction_echo",
)

EMPTY_FINAL_MARKERS = (
    "empty final answer",
    "low/empty final answer",
    "synth_empty",
    "deterministic fallback text",
)


@dataclass
class RunMetrics:
    request_id: str
    mode: str
    cost: float
    duration_ms: int
    warnings: list[str]


def _load_artifact_store(config_path: str):
    from orchestrator.config import load_config
    from orchestrator.observability.artifacts import ArtifactStore
    from orchestrator.security.encryption import build_envelope_cipher

    config = load_config(config_path)
    cipher = build_envelope_cipher(config.security.data_protection)
    return ArtifactStore(config.artifacts, cipher=cipher)


def _collect_recent_metrics(config_path: str, mode: str, limit: int) -> list[RunMetrics]:
    store = _load_artifact_store(config_path)
    rows = store.list_summaries(limit=max(20, limit * 4))
    out: list[RunMetrics] = []
    for row in rows:
        try:
            payload = store.load(row.request_id)
        except Exception:
            continue
        artifact = payload.get("artifact", {})
        if not isinstance(artifact, dict):
            continue
        run_mode = str(artifact.get("mode", "")).strip()
        if run_mode != mode:
            continue
        result = artifact.get("result", {})
        if not isinstance(result, dict):
            result = {}
        warnings_raw = result.get("warnings", [])
        warnings = [str(item) for item in warnings_raw] if isinstance(warnings_raw, list) else []
        out.append(
            RunMetrics(
                request_id=str(artifact.get("request_id", "")),
                mode=run_mode,
                cost=float(result.get("cost", artifact.get("cost_breakdown", {}).get("total_cost", 0.0)) or 0.0),
                duration_ms=int(artifact.get("duration_ms", 0) or 0),
                warnings=warnings,
            )
        )
        if len(out) >= limit:
            break
    return out


def _warning_matches(warning: str, markers: tuple[str, ...]) -> bool:
    text = warning.lower()
    return any(marker in text for marker in markers)


def _summarize(runs: list[RunMetrics]) -> dict[str, Any]:
    if not runs:
        return {
            "runs": 0,
            "low_signal": 0,
            "empty_final": 0,
            "avg_cost": 0.0,
            "avg_latency_ms": 0,
            "request_ids": [],
        }

    low_signal = 0
    empty_final = 0
    total_cost = 0.0
    total_latency = 0
    request_ids: list[str] = []

    for run in runs:
        request_ids.append(run.request_id)
        total_cost += run.cost
        total_latency += run.duration_ms
        if any(_warning_matches(warning, LOW_SIGNAL_MARKERS) for warning in run.warnings):
            low_signal += 1
        if any(_warning_matches(warning, EMPTY_FINAL_MARKERS) for warning in run.warnings):
            empty_final += 1

    return {
        "runs": len(runs),
        "low_signal": low_signal,
        "empty_final": empty_final,
        "avg_cost": round(total_cost / len(runs), 6),
        "avg_latency_ms": int(total_latency / len(runs)),
        "request_ids": request_ids,
    }


def _build_promote_cmd(args: argparse.Namespace, summary: dict[str, Any]) -> list[str]:
    return [
        sys.executable,
        "-m",
        "mmctl",
        "routes",
        "promote",
        "--config",
        args.config,
        "--profiles-file",
        args.profiles_file,
        "--runs",
        str(summary["runs"]),
        "--low-signal",
        str(summary["low_signal"]),
        "--empty-final",
        str(summary["empty_final"]),
        "--avg-cost",
        str(summary["avg_cost"]),
        "--avg-latency-ms",
        str(summary["avg_latency_ms"]),
        "--max-low-signal-rate",
        str(args.max_low_signal_rate),
        "--max-empty-final",
        str(args.max_empty_final),
        "--max-avg-cost",
        str(args.max_avg_cost),
        "--max-avg-latency-ms",
        str(args.max_avg_latency_ms),
        "--stable-profile",
        args.stable_profile,
        "--experimental-profile",
        args.experimental_profile,
    ]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compute promotion metrics from recent artifact runs and optionally execute mmctl routes promote."
    )
    parser.add_argument("--config", default="config/config.example.yaml")
    parser.add_argument("--mode", default="debate")
    parser.add_argument("--runs", type=int, default=10, help="Number of recent runs to include.")
    parser.add_argument("--profiles-file", default="evaluation/routes/profiles.json")
    parser.add_argument("--max-low-signal-rate", type=float, default=0.20)
    parser.add_argument("--max-empty-final", type=int, default=0)
    parser.add_argument("--max-avg-cost", type=float, default=0.01)
    parser.add_argument("--max-avg-latency-ms", type=int, default=120000)
    parser.add_argument("--stable-profile", default="stable")
    parser.add_argument("--experimental-profile", default="experimental")
    parser.add_argument("--out-file", default="evaluation/routes/promotion_metrics_latest.json")
    parser.add_argument("--apply", action="store_true", help="Execute mmctl routes promote with computed metrics.")
    args = parser.parse_args()

    if args.runs <= 0:
        raise SystemExit("--runs must be > 0")

    runs = _collect_recent_metrics(args.config, args.mode, args.runs)
    summary = _summarize(runs)
    if summary["runs"] < args.runs:
        print(
            f"warning: requested {args.runs} runs but only found {summary['runs']} mode='{args.mode}' artifacts",
            file=sys.stderr,
        )
    if summary["runs"] == 0:
        raise SystemExit("no matching artifacts found")

    out_payload = {
        "mode": args.mode,
        "summary": summary,
        "thresholds": {
            "max_low_signal_rate": args.max_low_signal_rate,
            "max_empty_final": args.max_empty_final,
            "max_avg_cost": args.max_avg_cost,
            "max_avg_latency_ms": args.max_avg_latency_ms,
        },
    }
    out_path = Path(args.out_file).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out_payload, indent=2, sort_keys=True), encoding="utf-8")

    print(json.dumps(out_payload, indent=2))

    cmd = _build_promote_cmd(args, summary)
    print("\nSuggested promote command:")
    print(" ".join(cmd))

    if args.apply:
        print("\nApplying promotion gate...")
        completed = subprocess.run(cmd, check=False)
        return int(completed.returncode)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
