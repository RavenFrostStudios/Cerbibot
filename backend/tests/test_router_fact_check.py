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


class FakeAdapter:
    def __init__(self, provider_name: str):
        self.provider_name = provider_name

    def count_tokens(self, text: str, model: str) -> int:
        return 5

    def estimate_cost(self, tokens_in: int, tokens_out: int, model: str) -> float:
        return 0.01

    async def complete(self, prompt: str, model: str, max_tokens: int, temperature: float) -> CompletionResult:
        return CompletionResult("answer", 10, 12, model, 1, 0.01, self.provider_name)

    async def complete_structured(self, prompt: str, model: str, output_schema: dict, max_tokens: int, temperature: float) -> CompletionResult:
        return CompletionResult('{"claims": ["Latest Python version is 3.13"]}', 1, 1, model, 1, 0.01, self.provider_name)

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
async def test_router_single_fact_check(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "y")
    monkeypatch.setattr("orchestrator.router.OpenAIAdapter", lambda _cfg: FakeAdapter("openai"))
    monkeypatch.setattr("orchestrator.router.AnthropicAdapter", lambda _cfg: FakeAdapter("anthropic"))

    async def _fake_run_fact_check(**_kwargs):
        from orchestrator.collaboration.fact_checker import VerifiedClaim

        return [
            VerifiedClaim(
                claim="Latest Python version is 3.13",
                classification="time_sensitive",
                verified=True,
                sources=["https://example.com/doc"],
                conflicts=[],
            )
        ]

    monkeypatch.setattr("orchestrator.router.run_fact_check", _fake_run_fact_check)

    orchestrator = Orchestrator(_config(tmp_path))
    result = await orchestrator.ask("test", mode="single", fact_check=True)
    assert result.verification_notes is not None
    assert len(result.verification_notes) == 1
    assert result.verification_notes[0].verified is True
