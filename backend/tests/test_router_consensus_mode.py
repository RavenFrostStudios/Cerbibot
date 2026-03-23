from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.config import (
    AppConfig,
    BudgetConfig,
    CritiqueRoutingConfig,
    ProviderConfig,
    ProviderModelConfig,
    ProviderPricing,
    RetrievalConfig,
    RoutingConfig,
    SecurityConfig,
)
from orchestrator.providers.base import CompletionResult
from orchestrator.router import Orchestrator


@pytest.fixture(autouse=True)
def _isolate_state_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MMO_STATE_DIR", str(tmp_path / "state"))


class FakeAdapter:
    def __init__(self, provider_name: str):
        self.provider_name = provider_name

    def count_tokens(self, text: str, model: str) -> int:
        return 5

    def estimate_cost(self, tokens_in: int, tokens_out: int, model: str) -> float:
        return 0.01

    async def complete(self, prompt: str, model: str, max_tokens: int, temperature: float) -> CompletionResult:
        return CompletionResult("answer", 10, 12, model, 1, 0.01, self.provider_name)

    async def complete_structured(
        self,
        prompt: str,
        model: str,
        output_schema: dict,
        max_tokens: int,
        temperature: float,
    ) -> CompletionResult:
        return CompletionResult("{}", 1, 1, model, 1, 0.01, self.provider_name)

    async def complete_stream(self, prompt: str, model: str, max_tokens: int, temperature: float):
        yield "answer"


def _config(tmp_path: Path) -> AppConfig:
    providers = {
        "openai": ProviderConfig(
            enabled=True,
            api_key_env="OPENAI_API_KEY",
            models=ProviderModelConfig(fast="m", deep="m"),
            pricing_usd_per_1m_tokens={"m": ProviderPricing(input=1.0, output=2.0)},
        ),
        "anthropic": ProviderConfig(
            enabled=True,
            api_key_env="ANTHROPIC_API_KEY",
            models=ProviderModelConfig(fast="m", deep="m"),
            pricing_usd_per_1m_tokens={"m": ProviderPricing(input=1.0, output=2.0)},
        ),
    }
    return AppConfig(
        default_mode="single",
        providers=providers,
        budgets=BudgetConfig(
            session_usd_cap=5.0,
            daily_usd_cap=5.0,
            monthly_usd_cap=5.0,
            usage_file=str(tmp_path / "usage.json"),
        ),
        security=SecurityConfig(
            block_on_secrets=True,
            redact_logs=True,
            tool_allowlist=[],
            retrieval_domain_allowlist=["example.com"],
            retrieval_domain_denylist=["localhost"],
        ),
        routing=RoutingConfig(
            critique=CritiqueRoutingConfig(
                drafter_provider="openai",
                critic_provider="anthropic",
                refiner_provider="openai",
            )
        ),
        retrieval=RetrievalConfig(
            search_provider="duckduckgo_html",
            max_results=3,
            max_fetch_bytes=10000,
            timeout_seconds=2.0,
        ),
    )


@pytest.mark.asyncio
async def test_router_consensus_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "y")
    monkeypatch.setattr("orchestrator.router.OpenAIAdapter", lambda _cfg: FakeAdapter("openai"))
    monkeypatch.setattr("orchestrator.router.AnthropicAdapter", lambda _cfg: FakeAdapter("anthropic"))

    class _W:
        final_answer = "Canberra"
        answers_by_provider = {"openai": "Canberra", "anthropic": "Canberra"}
        agreement_score = 0.8
        confidence = 0.84
        used_adjudication = False
        adjudication_reason = None
        citations = []
        total_cost = 0.02
        total_tokens_in = 20
        total_tokens_out = 24
        models = ["m", "m"]
        warnings = []

    async def _fake_run_consensus_workflow(**_kwargs):
        return _W()

    monkeypatch.setattr("orchestrator.router.run_consensus_workflow", _fake_run_consensus_workflow)

    orchestrator = Orchestrator(_config(tmp_path))
    result = await orchestrator.ask("capital of australia", mode="consensus", verbose=True)
    assert result.mode == "consensus"
    assert result.answer == "Canberra"
    assert result.consensus_answers is not None
    assert result.consensus_confidence == 0.84
