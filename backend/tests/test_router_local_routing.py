from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.config import (
    AppConfig,
    BudgetConfig,
    CritiqueRoutingConfig,
    LocalRoutingConfig,
    ProviderConfig,
    ProviderModelConfig,
    ProviderPricing,
    RetrievalConfig,
    RoutingConfig,
    SecurityConfig,
)
from orchestrator.providers.base import CompletionResult
from orchestrator.rate_limiter import RateLimitExceededError
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
        return CompletionResult(f"{self.provider_name}-answer", 10, 12, model, 1, 0.01, self.provider_name)

    async def complete_structured(self, prompt: str, model: str, output_schema: dict, max_tokens: int, temperature: float) -> CompletionResult:
        return CompletionResult("{}", 1, 1, model, 1, 0.01, self.provider_name)

    async def complete_stream(self, prompt: str, model: str, max_tokens: int, temperature: float):
        yield f"{self.provider_name}-answer"


class FakeRateLimitedAdapter(FakeAdapter):
    async def complete(self, prompt: str, model: str, max_tokens: int, temperature: float) -> CompletionResult:
        raise RateLimitExceededError("wait exceeded")

    async def complete_stream(self, prompt: str, model: str, max_tokens: int, temperature: float):
        raise RateLimitExceededError("wait exceeded")
        yield ""


def _config(tmp_path: Path) -> AppConfig:
    providers = {
        "openai": ProviderConfig(
            enabled=True,
            api_key_env="OPENAI_API_KEY",
            models=ProviderModelConfig(fast="m", deep="m"),
            pricing_usd_per_1m_tokens={"m": ProviderPricing(input=1.0, output=2.0)},
        ),
        "local": ProviderConfig(
            enabled=True,
            api_key_env="LOCAL_API_KEY",
            models=ProviderModelConfig(fast="lm", deep="lm"),
            pricing_usd_per_1m_tokens={"lm": ProviderPricing(input=0.0, output=0.0)},
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
            retrieval_domain_allowlist=[],
            retrieval_domain_denylist=[],
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
        local_routing=LocalRoutingConfig(enabled=True, local_provider_name="local", quality_threshold=0.65),
    )


@pytest.mark.asyncio
async def test_router_uses_local_for_low_stakes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("LOCAL_API_KEY", "x")
    monkeypatch.setattr("orchestrator.router.OpenAIAdapter", lambda _cfg: FakeAdapter("openai"))
    monkeypatch.setattr("orchestrator.router.LocalAdapter", lambda _cfg: FakeAdapter("local"))
    monkeypatch.setattr("orchestrator.router.AnthropicAdapter", lambda _cfg: FakeAdapter("anthropic"))
    orchestrator = Orchestrator(_config(tmp_path))
    result = await orchestrator.ask("Explain what a list comprehension is.", mode="single")
    assert result.provider == "local"


@pytest.mark.asyncio
async def test_router_uses_cloud_for_high_stakes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("LOCAL_API_KEY", "x")
    monkeypatch.setattr("orchestrator.router.OpenAIAdapter", lambda _cfg: FakeAdapter("openai"))
    monkeypatch.setattr("orchestrator.router.LocalAdapter", lambda _cfg: FakeAdapter("local"))
    monkeypatch.setattr("orchestrator.router.AnthropicAdapter", lambda _cfg: FakeAdapter("anthropic"))
    orchestrator = Orchestrator(_config(tmp_path))
    result = await orchestrator.ask("What is the latest legal regulation update for HIPAA compliance?", mode="single")
    assert result.provider == "openai"


@pytest.mark.asyncio
async def test_router_falls_back_when_selected_provider_rate_limited(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("LOCAL_API_KEY", "x")
    monkeypatch.setattr("orchestrator.router.OpenAIAdapter", lambda _cfg, **_kwargs: FakeRateLimitedAdapter("openai"))
    monkeypatch.setattr("orchestrator.router.LocalAdapter", lambda _cfg, **_kwargs: FakeAdapter("local"))
    monkeypatch.setattr("orchestrator.router.AnthropicAdapter", lambda _cfg, **_kwargs: FakeAdapter("anthropic"))
    orchestrator = Orchestrator(_config(tmp_path))
    result = await orchestrator.ask(
        "What is the latest legal regulation update for HIPAA compliance?",
        mode="single",
        provider="openai",
    )
    assert result.provider == "local"
    assert result.warnings and any("Rate-limited provider openai" in item for item in result.warnings)


@pytest.mark.asyncio
async def test_router_uses_rate_headroom_for_selection(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("LOCAL_API_KEY", "x")
    monkeypatch.setattr("orchestrator.router.OpenAIAdapter", lambda _cfg, **_kwargs: FakeAdapter("openai"))
    monkeypatch.setattr("orchestrator.router.LocalAdapter", lambda _cfg, **_kwargs: FakeAdapter("local"))
    monkeypatch.setattr("orchestrator.router.AnthropicAdapter", lambda _cfg, **_kwargs: FakeAdapter("anthropic"))
    orchestrator = Orchestrator(_config(tmp_path))

    def _headroom(name: str) -> dict[str, float]:
        if name == "openai":
            return {"rpm_headroom": 0.0, "tpm_headroom": 0.0}
        return {"rpm_headroom": 1.0, "tpm_headroom": 1.0}

    monkeypatch.setattr(orchestrator.rate_limiter, "headroom", _headroom)
    result = await orchestrator.ask("What is the latest legal regulation update for HIPAA compliance?", mode="single")
    assert result.provider == "local"


@pytest.mark.asyncio
async def test_auto_mode_switches_between_single_retrieval_and_critique(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("LOCAL_API_KEY", "x")
    monkeypatch.setattr("orchestrator.router.OpenAIAdapter", lambda _cfg, **_kwargs: FakeAdapter("openai"))
    monkeypatch.setattr("orchestrator.router.LocalAdapter", lambda _cfg, **_kwargs: FakeAdapter("local"))
    monkeypatch.setattr("orchestrator.router.AnthropicAdapter", lambda _cfg, **_kwargs: FakeAdapter("anthropic"))
    orchestrator = Orchestrator(_config(tmp_path))

    monkeypatch.setattr(
        orchestrator.budgets,
        "remaining",
        lambda: {"session": 100.0, "daily": 100.0, "monthly": 100.0},
    )
    assert orchestrator._auto_mode_for_query("What is Python?") == "single"
    assert orchestrator._auto_mode_for_query("What are the latest HIPAA rules today?") == "retrieval"
    assert orchestrator._auto_mode_for_query("What time is it in London right now?") == "retrieval"
    assert orchestrator._auto_mode_for_query("What is the current price of bitcoin?") == "retrieval"
    assert orchestrator._auto_mode_for_query("Design a microservice architecture and compare tradeoffs in detail") == "critique"


@pytest.mark.asyncio
async def test_auto_mode_ignores_context_wrapper_tokens(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("LOCAL_API_KEY", "x")
    monkeypatch.setattr("orchestrator.router.OpenAIAdapter", lambda _cfg, **_kwargs: FakeAdapter("openai"))
    monkeypatch.setattr("orchestrator.router.LocalAdapter", lambda _cfg, **_kwargs: FakeAdapter("local"))
    monkeypatch.setattr("orchestrator.router.AnthropicAdapter", lambda _cfg, **_kwargs: FakeAdapter("anthropic"))
    orchestrator = Orchestrator(_config(tmp_path))

    composed = (
        "Conversation context:\n- user: hello\n\nCurrent user turn:\ncan you see what I'm typing?"
    )
    assert orchestrator._auto_mode_for_query(composed) == "single"


def test_resolve_confirmed_web_query_uses_last_user_query(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("LOCAL_API_KEY", "x")
    monkeypatch.setattr("orchestrator.router.OpenAIAdapter", lambda _cfg, **_kwargs: FakeAdapter("openai"))
    monkeypatch.setattr("orchestrator.router.LocalAdapter", lambda _cfg, **_kwargs: FakeAdapter("local"))
    monkeypatch.setattr("orchestrator.router.AnthropicAdapter", lambda _cfg, **_kwargs: FakeAdapter("anthropic"))
    orchestrator = Orchestrator(_config(tmp_path))

    context_messages = [
        {"role": "user", "content": "Find a good website for free LLM models."},
        {"role": "assistant", "content": "Reply with yes search web to continue."},
    ]
    resolved = orchestrator._resolve_confirmed_web_query("yes search web", context_messages)
    assert resolved == "Find a good website for free LLM models."


def test_extract_user_turn_allows_yes_search_web_confirmation_with_profile_wrapper(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("LOCAL_API_KEY", "x")
    monkeypatch.setattr("orchestrator.router.OpenAIAdapter", lambda _cfg, **_kwargs: FakeAdapter("openai"))
    monkeypatch.setattr("orchestrator.router.LocalAdapter", lambda _cfg, **_kwargs: FakeAdapter("local"))
    monkeypatch.setattr("orchestrator.router.AnthropicAdapter", lambda _cfg, **_kwargs: FakeAdapter("anthropic"))
    orchestrator = Orchestrator(_config(tmp_path))

    wrapped = (
        "Assistant profile (follow these instructions exactly for this response):\n- Name: CerbiBot\n\n"
        "User request:\nyes search web"
    )
    context_messages = [
        {"role": "user", "content": "what time is it in london right now"},
        {"role": "assistant", "content": "Reply with yes search web to continue."},
    ]
    extracted = orchestrator._extract_user_turn_for_retrieval(wrapped)
    resolved = orchestrator._resolve_confirmed_web_query(extracted, context_messages)
    assert resolved == "what time is it in london right now"
