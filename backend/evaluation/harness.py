from __future__ import annotations

import json
from pathlib import Path
from time import perf_counter

import yaml

from orchestrator.router import Orchestrator


def load_tasks(tasks_dir: str) -> list[dict]:
    files = sorted(Path(tasks_dir).glob("*.yaml"))
    tasks: list[dict] = []
    for file in files:
        loaded = yaml.safe_load(file.read_text())
        if isinstance(loaded, list):
            tasks.extend(loaded)
    return tasks


async def run_eval(orchestrator: Orchestrator, tasks_dir: str, out_dir: str) -> dict:
    tasks = load_tasks(tasks_dir)
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    summary = {"total": len(tasks), "single": [], "critique": []}
    for task in tasks:
        for mode in ("single", "critique"):
            start = perf_counter()
            result = await orchestrator.ask(task["query"], mode=mode)
            elapsed_ms = int((perf_counter() - start) * 1000)
            entry = {
                "id": task["id"],
                "mode": mode,
                "query": task["query"],
                "answer": result.answer,
                "tokens_in": result.tokens_in,
                "tokens_out": result.tokens_out,
                "cost": result.cost,
                "latency_ms": elapsed_ms,
            }
            summary[mode].append(entry)

    outfile = out_path / "latest_eval.json"
    outfile.write_text(json.dumps(summary, indent=2))
    return summary
