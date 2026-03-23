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
from orchestrator.memory.store import MemoryStore
from orchestrator.providers.base import CompletionResult
from orchestrator.router import Orchestrator


class CaptureAdapter:
    def __init__(self, provider_name: str):
        self.provider_name = provider_name
        self.last_prompt = ""

    def count_tokens(self, text: str, model: str) -> int:
        return 5

    def estimate_cost(self, tokens_in: int, tokens_out: int, model: str) -> float:
        return 0.01

    async def complete(self, prompt: str, model: str, max_tokens: int, temperature: float) -> CompletionResult:
        self.last_prompt = prompt
        return CompletionResult("ok", 10, 10, model, 1, 0.01, self.provider_name)

    async def complete_structured(self, prompt: str, model: str, output_schema: dict, max_tokens: int, temperature: float) -> CompletionResult:
        return CompletionResult("{}", 1, 1, model, 1, 0.01, self.provider_name)

    async def complete_stream(self, prompt: str, model: str, max_tokens: int, temperature: float):
        yield "ok"


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        default_mode="single",
        providers={
            "openai": ProviderConfig(
                enabled=True,
                api_key_env="OPENAI_API_KEY",
                models=ProviderModelConfig(fast="m", deep="m"),
                pricing_usd_per_1m_tokens={"m": ProviderPricing(input=1.0, output=1.0)},
            ),
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
    )


@pytest.mark.asyncio
async def test_router_includes_memory_context(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    adapter = CaptureAdapter("openai")
    monkeypatch.setattr("orchestrator.router.OpenAIAdapter", lambda _cfg: adapter)
    monkeypatch.setattr("orchestrator.router.AnthropicAdapter", lambda _cfg: adapter)

    orchestrator = Orchestrator(_config(tmp_path))
    orchestrator.providers = {"openai": adapter}
    store = MemoryStore(str(tmp_path / "memory.db"))
    store.add(
        statement="User likes concise answers",
        source_type="user_preference",
        source_ref="manual",
        confidence=0.9,
        ttl_days=30,
    )
    orchestrator.memory_store = store

    await orchestrator.ask("please be concise", mode="single")
    assert "Governed memory context" in adapter.last_prompt
    assert "User likes concise answers" in adapter.last_prompt


@pytest.mark.asyncio
async def test_router_suppresses_memory_context_for_web_style_query(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    adapter = CaptureAdapter("openai")
    monkeypatch.setattr("orchestrator.router.OpenAIAdapter", lambda _cfg: adapter)
    monkeypatch.setattr("orchestrator.router.AnthropicAdapter", lambda _cfg: adapter)

    orchestrator = Orchestrator(_config(tmp_path))
    orchestrator.providers = {"openai": adapter}
    store = MemoryStore(str(tmp_path / "memory.db"))
    store.add(
        statement="RAVEN-APPLE-42",
        source_type="user_request",
        source_ref="chat.session:test",
        confidence=0.9,
        ttl_days=30,
    )
    orchestrator.memory_store = store

    await orchestrator.ask("can you look up a good website for free llm models", mode="single")
    assert "Governed memory context" not in adapter.last_prompt
    assert "RAVEN-APPLE-42" not in adapter.last_prompt


@pytest.mark.asyncio
async def test_router_filters_irrelevant_session_memory_from_wrapped_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    adapter = CaptureAdapter("openai")
    monkeypatch.setattr("orchestrator.router.OpenAIAdapter", lambda _cfg: adapter)
    monkeypatch.setattr("orchestrator.router.AnthropicAdapter", lambda _cfg: adapter)

    orchestrator = Orchestrator(_config(tmp_path))
    orchestrator.providers = {"openai": adapter}
    store = MemoryStore(str(tmp_path / "memory.db"))
    store.add(
        statement="RAVEN-APPLE-41",
        source_type="user_request",
        source_ref="chat.session:old",
        confidence=0.9,
        ttl_days=30,
    )
    orchestrator.memory_store = store

    await orchestrator.ask("please answer in one short sentence about koalas.", mode="single")
    assert "Governed memory context" not in adapter.last_prompt
    assert "RAVEN-APPLE-41" not in adapter.last_prompt
