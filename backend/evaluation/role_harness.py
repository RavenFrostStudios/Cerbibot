from __future__ import annotations

import json
from pathlib import Path
from statistics import mean
from typing import Any

import yaml

from orchestrator.collaboration.output_parser import parse_structured_output
from orchestrator.collaboration.quality_gates import (
    is_low_signal_by_quality,
    is_placeholder_response,
    score_answer_quality,
)
from orchestrator.providers.base import ProviderAdapter
from orchestrator.router import Orchestrator


_ROLE_SCHEMAS: dict[str, dict[str, str]] = {
    "drafter": {"answer": "string", "assumptions": "array", "needs_verification": "array"},
    "critic": {"issues": "array", "missing": "array", "risk_flags": "array"},
    "refiner": {"final_answer": "string", "citations": "array", "confidence": "string"},
}

_ROLE_REQUIRED_KEYS: dict[str, list[str]] = {
    "drafter": ["answer", "assumptions", "needs_verification"],
    "critic": ["issues", "missing", "risk_flags"],
    "refiner": ["final_answer", "citations", "confidence"],
}


def load_role_fixtures(path: str) -> list[dict[str, str]]:
    fixture_path = Path(path)
    if not fixture_path.exists():
        raise FileNotFoundError(f"Role eval fixture file not found: {path}")
    payload = yaml.safe_load(fixture_path.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not payload:
        raise ValueError("Role eval fixtures must be a non-empty YAML list")
    normalized: list[dict[str, str]] = []
    for idx, item in enumerate(payload):
        if not isinstance(item, dict):
            continue
        query = str(item.get("query", "")).strip()
        if not query:
            continue
        normalized.append(
            {
                "id": str(item.get("id", f"fixture-{idx+1}")).strip() or f"fixture-{idx+1}",
                "query": query,
                "draft": str(item.get("draft", "")).strip(),
                "critique": str(item.get("critique", "")).strip(),
            }
        )
    if not normalized:
        raise ValueError("Role eval fixtures contain no usable query entries")
    return normalized


def _build_role_prompt(role: str, fixture: dict[str, str]) -> str:
    query = fixture["query"]
    draft = fixture.get("draft", "") or "Draft unavailable; infer likely draft and critique it."
    critique = fixture.get("critique", "") or "Issues: none\nMissing: none\nRisk Flags: none"

    if role == "drafter":
        return f"You are a precise drafter. Question: {query}"
    if role == "critic":
        return (
            "You are a strict critic. Return JSON only.\n"
            f"Query:\n{query}\n"
            f"Draft:\n{draft}\n"
        )
    if role == "refiner":
        return (
            "You are a strict refiner. Return JSON only.\n"
            f"Query:\n{query}\n"
            f"Draft:\n{draft}\n"
            f"Critique:\n{critique}\n"
        )
    raise ValueError(f"Unsupported role: {role}")


def _extract_answer_text(role: str, parsed_payload: dict[str, Any], raw_text: str) -> str:
    if role == "drafter":
        return str(parsed_payload.get("answer", "")).strip() or raw_text
    if role == "refiner":
        return str(parsed_payload.get("final_answer", "")).strip() or raw_text
    if role == "critic":
        issues = parsed_payload.get("issues", [])
        missing = parsed_payload.get("missing", [])
        risks = parsed_payload.get("risk_flags", [])
        if isinstance(issues, list) and isinstance(missing, list) and isinstance(risks, list):
            text = (
                "Issues: "
                + "; ".join(str(i) for i in issues if str(i).strip())
                + "\nMissing: "
                + "; ".join(str(i) for i in missing if str(i).strip())
                + "\nRisk Flags: "
                + "; ".join(str(i) for i in risks if str(i).strip())
            ).strip()
            return text or raw_text
    return raw_text


def _cost_efficiency(avg_cost: float) -> float:
    # Lower per-call cost should improve the score but never dominate quality.
    return 1.0 / (1.0 + max(0.0, avg_cost) * 2000.0)


def compute_candidate_score(metrics: dict[str, float], strategy: str) -> float:
    avg_quality = float(metrics.get("avg_quality", 0.0))
    valid_rate = float(metrics.get("json_valid_rate", 0.0))
    low_signal_rate = float(metrics.get("low_signal_rate", 1.0))
    placeholder_rate = float(metrics.get("placeholder_rate", 1.0))
    error_rate = float(metrics.get("error_rate", 1.0))
    avg_cost = float(metrics.get("avg_cost", 0.0))

    content = (
        0.45 * avg_quality
        + 0.25 * valid_rate
        + 0.15 * (1.0 - low_signal_rate)
        + 0.15 * (1.0 - placeholder_rate)
    )
    reliability = max(0.0, 1.0 - error_rate)
    quality_base = max(0.0, min(1.0, content * reliability))
    efficiency = _cost_efficiency(avg_cost)

    if strategy == "quality":
        return max(0.0, min(1.0, (0.85 * quality_base) + (0.15 * efficiency)))
    if strategy == "cost":
        return max(0.0, min(1.0, (0.40 * quality_base) + (0.60 * efficiency)))
    return max(0.0, min(1.0, (0.65 * quality_base) + (0.35 * efficiency)))


def rank_role_candidates(candidates: list[dict[str, Any]], strategy: str) -> list[dict[str, Any]]:
    ranked: list[dict[str, Any]] = []
    for candidate in candidates:
        with_score = dict(candidate)
        with_score["score"] = compute_candidate_score(candidate, strategy)
        ranked.append(with_score)
    ranked.sort(
        key=lambda item: (
            float(item.get("score", 0.0)),
            float(item.get("json_valid_rate", 0.0)),
            -float(item.get("avg_cost", 0.0)),
        ),
        reverse=True,
    )
    return ranked


def build_recommended_routes(existing_routes: dict[str, Any], winners: dict[str, str]) -> dict[str, Any]:
    next_routes = json.loads(json.dumps(existing_routes))
    critique = next_routes.get("critique")
    if not isinstance(critique, dict):
        critique = {}
        next_routes["critique"] = critique
    if winners.get("drafter"):
        critique["drafter_provider"] = winners["drafter"]
    if winners.get("critic"):
        critique["critic_provider"] = winners["critic"]
    if winners.get("refiner"):
        critique["refiner_provider"] = winners["refiner"]
    return next_routes


async def _evaluate_role_provider(
    *,
    adapter: ProviderAdapter,
    provider_name: str,
    model: str,
    role: str,
    fixtures: list[dict[str, str]],
) -> dict[str, Any]:
    schema = _ROLE_SCHEMAS[role]
    required = _ROLE_REQUIRED_KEYS[role]

    errors = 0
    valids: list[float] = []
    low_signals: list[float] = []
    placeholders: list[float] = []
    qualities: list[float] = []
    latencies_ms: list[float] = []
    costs: list[float] = []
    sample_errors: list[str] = []

    for fixture in fixtures:
        prompt = _build_role_prompt(role, fixture)
        try:
            result = await adapter.complete_structured(
                prompt=prompt,
                model=model,
                output_schema=schema,
                max_tokens=700,
                temperature=0.1,
            )
            parsed = parse_structured_output(result.text, required)
            payload = parsed.data if isinstance(parsed.data, dict) else {}
            answer_text = _extract_answer_text(role, payload, result.text)
            quality_score, _ = score_answer_quality(answer_text, user_query=fixture["query"], min_words=30)

            valids.append(1.0 if parsed.valid else 0.0)
            placeholders.append(1.0 if is_placeholder_response(answer_text) else 0.0)
            low_signals.append(1.0 if is_low_signal_by_quality(answer_text, user_query=fixture["query"]) else 0.0)
            qualities.append(float(quality_score))
            latencies_ms.append(float(result.latency_ms))
            costs.append(float(result.estimated_cost))
        except Exception as exc:  # pragma: no cover - network/provider variability
            errors += 1
            if len(sample_errors) < 3:
                sample_errors.append(str(exc))

    total = len(fixtures)
    successful = max(0, total - errors)
    return {
        "role": role,
        "provider": provider_name,
        "model": model,
        "samples_total": total,
        "samples_succeeded": successful,
        "samples_failed": errors,
        "json_valid_rate": mean(valids) if valids else 0.0,
        "placeholder_rate": mean(placeholders) if placeholders else 1.0,
        "low_signal_rate": mean(low_signals) if low_signals else 1.0,
        "avg_quality": mean(qualities) if qualities else 0.0,
        "avg_latency_ms": mean(latencies_ms) if latencies_ms else 0.0,
        "avg_cost": mean(costs) if costs else 0.0,
        "error_rate": (errors / total) if total else 1.0,
        "sample_errors": sample_errors,
    }


async def run_role_eval(
    *,
    orchestrator: Orchestrator,
    fixtures_path: str,
    out_file: str,
    strategy: str = "balanced",
    apply_best: bool = False,
) -> dict[str, Any]:
    fixtures = load_role_fixtures(fixtures_path)
    if not orchestrator.providers:
        raise ValueError("No enabled providers available for role evaluation")

    enabled = sorted(orchestrator.providers.keys())
    role_results: dict[str, list[dict[str, Any]]] = {"drafter": [], "critic": [], "refiner": []}

    for role in ("drafter", "critic", "refiner"):
        for provider_name in enabled:
            adapter = orchestrator.providers[provider_name]
            provider_cfg = orchestrator.config.providers[provider_name]
            model = str(provider_cfg.models.deep)
            metrics = await _evaluate_role_provider(
                adapter=adapter,
                provider_name=provider_name,
                model=model,
                role=role,
                fixtures=fixtures,
            )
            role_results[role].append(metrics)

    ranked = {role: rank_role_candidates(candidates, strategy) for role, candidates in role_results.items()}
    winners: dict[str, str] = {}
    for role in ("drafter", "critic", "refiner"):
        top = ranked[role][0] if ranked[role] else {}
        if int(top.get("samples_succeeded", 0)) > 0:
            winners[role] = str(top.get("provider", ""))

    current_routes = orchestrator.get_role_routes()
    recommended_routes = build_recommended_routes(current_routes, winners)

    applied_routes = None
    if apply_best:
        applied_routes = orchestrator.apply_role_routes(recommended_routes)

    summary = {
        "strategy": strategy,
        "fixtures_path": fixtures_path,
        "fixtures_total": len(fixtures),
        "enabled_providers": enabled,
        "ranked": ranked,
        "winners": winners,
        "recommended_routes": recommended_routes,
        "applied_routes": applied_routes,
    }

    out_path = Path(out_file)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary
