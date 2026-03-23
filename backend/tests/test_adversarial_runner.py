from __future__ import annotations

import json
from pathlib import Path

import pytest

from evaluation.adversarial.runner import load_adversarial_fixtures, run_adversarial_eval
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
        return CompletionResult("safe response", 10, 10, model, 1, 0.01, self.provider_name)

    async def complete_structured(self, prompt: str, model: str, output_schema: dict, max_tokens: int, temperature: float) -> CompletionResult:
        return CompletionResult("{}", 1, 1, model, 1, 0.01, self.provider_name)

    async def complete_stream(self, prompt: str, model: str, max_tokens: int, temperature: float):
        yield "safe"


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        default_mode="single",
        providers={
            "openai": ProviderConfig(
                enabled=True,
                api_key_env="OPENAI_API_KEY",
                models=ProviderModelConfig(fast="m", deep="m"),
                pricing_usd_per_1m_tokens={"m": ProviderPricing(input=1.0, output=1.0)},
            )
        },
        budgets=BudgetConfig(
            session_usd_cap=10.0,
            daily_usd_cap=10.0,
            monthly_usd_cap=10.0,
            usage_file=str(tmp_path / "usage.json"),
        ),
        security=SecurityConfig(
            block_on_secrets=True,
            redact_logs=True,
            tool_allowlist=["fetch_url", "web_search"],
            retrieval_domain_allowlist=["example.com"],
            retrieval_domain_denylist=["localhost"],
        ),
        routing=RoutingConfig(
            critique=CritiqueRoutingConfig(
                drafter_provider="openai",
                critic_provider="openai",
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


def test_load_adversarial_fixtures() -> None:
    fixtures = load_adversarial_fixtures("evaluation/adversarial")
    assert fixtures
    assert any(item["id"] == "adv-injection-1" for item in fixtures)


@pytest.mark.asyncio
async def test_run_adversarial_eval(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setattr("orchestrator.router.OpenAIAdapter", lambda _cfg: FakeAdapter("openai"))
    monkeypatch.setattr("orchestrator.router.AnthropicAdapter", lambda _cfg: FakeAdapter("anthropic"))
    orchestrator = Orchestrator(_config(tmp_path))

    out_file = tmp_path / "adversarial.json"
    summary = await run_adversarial_eval(orchestrator, "evaluation/adversarial", str(out_file))
    assert summary["total"] >= 6
    assert out_file.exists()
    saved = json.loads(out_file.read_text(encoding="utf-8"))
    assert saved["total"] == summary["total"]
