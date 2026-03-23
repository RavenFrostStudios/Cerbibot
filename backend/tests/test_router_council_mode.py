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

    async def complete_structured(self, prompt: str, model: str, output_schema: dict, max_tokens: int, temperature: float) -> CompletionResult:
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
async def test_router_council_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MMO_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "y")
    monkeypatch.setattr("orchestrator.router.OpenAIAdapter", lambda _cfg: FakeAdapter("openai"))
    monkeypatch.setattr("orchestrator.router.AnthropicAdapter", lambda _cfg: FakeAdapter("anthropic"))

    class _W:
        final_answer = "council answer"
        specialists = [
            type("S", (), {"role": "coding", "text": "coding text"})(),
            type("S", (), {"role": "security", "text": "security text"})(),
        ]
        synthesis_notes = "merged"
        total_cost = 0.03
        total_tokens_in = 30
        total_tokens_out = 30
        models = ["m", "m", "m"]
        warnings = []

    async def _fake_run_council_workflow(**_kwargs):
        return _W()

    monkeypatch.setattr("orchestrator.router.run_council_workflow", _fake_run_council_workflow)

    orchestrator = Orchestrator(_config(tmp_path))
    result = await orchestrator.ask("design auth", mode="council", verbose=True)
    assert result.mode == "council"
    assert result.answer == "council answer"
    assert result.council_outputs is not None
    assert result.council_notes == "merged"


@pytest.mark.asyncio
async def test_router_council_mode_empty_synthesis_uses_specialist_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MMO_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "y")
    monkeypatch.setattr("orchestrator.router.OpenAIAdapter", lambda _cfg: FakeAdapter("openai"))
    monkeypatch.setattr("orchestrator.router.AnthropicAdapter", lambda _cfg: FakeAdapter("anthropic"))

    class _W:
        final_answer = ""
        specialists = [
            type("S", (), {"role": "coding", "text": '{"answer":"Partially accurate review","claims":["x"]}'})(),
            type("S", (), {"role": "security", "text": "security text"})(),
        ]
        synthesis_notes = "merged"
        total_cost = 0.03
        total_tokens_in = 30
        total_tokens_out = 30
        models = ["m", "m", "m"]
        warnings = []

    async def _fake_run_council_workflow(**_kwargs):
        return _W()

    monkeypatch.setattr("orchestrator.router.run_council_workflow", _fake_run_council_workflow)

    orchestrator = Orchestrator(_config(tmp_path))
    result = await orchestrator.ask("design auth", mode="council", verbose=True)
    assert result.mode == "council"
    assert "security text" in result.answer
    assert "Partially accurate review" not in result.answer
    assert result.warnings is not None
    assert any("deterministic local council synthesis" in item for item in result.warnings)


@pytest.mark.asyncio
async def test_router_council_mode_single_provider_fast_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MMO_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "y")
    monkeypatch.setattr("orchestrator.router.OpenAIAdapter", lambda _cfg: FakeAdapter("openai"))
    monkeypatch.setattr("orchestrator.router.AnthropicAdapter", lambda _cfg: FakeAdapter("anthropic"))

    cfg = _config(tmp_path)

    async def _should_not_run(**_kwargs):
        raise AssertionError("run_council_workflow should not run in council single-provider fast path")

    monkeypatch.setattr("orchestrator.router.run_council_workflow", _should_not_run)

    orchestrator = Orchestrator(cfg)
    orchestrator.apply_role_routes(
        {
            "council": {
                "specialist_roles": {
                    "coding": "openai",
                    "security": "openai",
                    "writing": "openai",
                    "factual": "openai",
                },
                "synthesizer_provider": "openai",
            }
        }
    )
    result = await orchestrator.ask("design auth", mode="council", verbose=True)
    assert result.mode == "council"
    assert result.provider == "openai"
    assert result.answer == "answer"
    assert result.warnings is not None
    assert any("Council optimized to single-pass" in item for item in result.warnings)
