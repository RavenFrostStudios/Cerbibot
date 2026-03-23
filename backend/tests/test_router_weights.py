from __future__ import annotations

from pathlib import Path

from orchestrator.router_weights import RouterWeights, classify_domain


def test_router_weights_learn_and_persist(tmp_path: Path) -> None:
    path = tmp_path / "router_weights.json"
    weights = RouterWeights(providers=["openai", "anthropic"], weights_file=str(path), learning_rate=0.5)
    weights.record_success(provider="openai", domain="coding", quality=0.95, cost=0.01, latency_ms=900)
    weights.record_success(provider="anthropic", domain="coding", quality=0.60, cost=0.05, latency_ms=2200, had_warning=True)
    ranked = weights.rank(["openai", "anthropic"], "coding")
    assert ranked[0] == "openai"

    loaded = RouterWeights(providers=["openai", "anthropic"], weights_file=str(path), learning_rate=0.5)
    snap = loaded.snapshot()
    assert snap["openai"]["coding"]["count"] >= 1
    assert snap["openai"]["coding"]["score"] >= snap["anthropic"]["coding"]["score"]


def test_classify_domain_keywords() -> None:
    assert classify_domain("Write Python code for an API client.") == "coding"
    assert classify_domain("What is the latest regulation update today?") == "factual"
    assert classify_domain("Find security vulnerabilities in this auth flow.") == "security"
