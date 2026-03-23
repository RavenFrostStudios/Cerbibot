from __future__ import annotations

import json
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
        return CompletionResult("final with tools", 10, 12, model, 1, 0.01, self.provider_name)

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
        yield "final with tools"


def _config(tmp_path: Path) -> AppConfig:
    providers = {
        "openai": ProviderConfig(
            enabled=True,
            api_key_env="OPENAI_API_KEY",
            models=ProviderModelConfig(fast="m", deep="m"),
            pricing_usd_per_1m_tokens={"m": ProviderPricing(input=1.0, output=2.0)},
        ),
        "anthropic": ProviderConfig(
            enabled=False,
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
async def test_router_single_with_tools(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setattr("orchestrator.router.OpenAIAdapter", lambda _cfg: FakeAdapter("openai"))
    monkeypatch.setattr("orchestrator.router.AnthropicAdapter", lambda _cfg: FakeAdapter("anthropic"))

    class _Parsed:
        valid = True
        error = None
        data = {
            "use_tool": True,
            "tool_name": "python_exec",
            "args_json": json.dumps({"code": "print(8)"}),
            "reason": "needs computation",
        }

    async def _fake_structured_retry(**_kwargs):
        return CompletionResult("{}", 1, 1, "m", 1, 0.01, "openai"), _Parsed()

    def _fake_execute_tool(_manifest, _scope, _guardian):
        return {
            "status": "ok",
            "tool": "python_exec",
            "stdout": "8\n",
            "stderr": "",
            "exit_code": 0,
            "timed_out": False,
            "backend": "docker",
            "warning": None,
            "security_warnings": [],
        }

    monkeypatch.setattr("orchestrator.router._call_structured_with_retry", _fake_structured_retry)
    monkeypatch.setattr("orchestrator.router.execute_tool", _fake_execute_tool)

    orchestrator = Orchestrator(_config(tmp_path))
    result = await orchestrator.ask("calculate", mode="single", tools="run python code")
    assert result.mode == "single"
    assert result.tool_outputs is not None
    assert result.tool_outputs[0]["tool"] == "python_exec"


@pytest.mark.asyncio
async def test_router_rejects_tools_outside_single_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setattr("orchestrator.router.OpenAIAdapter", lambda _cfg: FakeAdapter("openai"))
    monkeypatch.setattr("orchestrator.router.AnthropicAdapter", lambda _cfg: FakeAdapter("anthropic"))
    orchestrator = Orchestrator(_config(tmp_path))
    with pytest.raises(ValueError, match="single mode"):
        await orchestrator.ask("x", mode="consensus", tools="run python code")


@pytest.mark.asyncio
async def test_router_rejects_tool_when_intent_drift_detected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setattr("orchestrator.router.OpenAIAdapter", lambda _cfg: FakeAdapter("openai"))
    monkeypatch.setattr("orchestrator.router.AnthropicAdapter", lambda _cfg: FakeAdapter("anthropic"))

    class _Parsed:
        valid = True
        error = None
        data = {
            "use_tool": True,
            "tool_name": "python_exec",
            "args_json": json.dumps({"code": "import os\nprint(os.listdir('/'))"}),
            "reason": "enumerate root filesystem",
        }

    async def _fake_structured_retry(**_kwargs):
        return CompletionResult("{}", 1, 1, "m", 1, 0.01, "openai"), _Parsed()

    monkeypatch.setattr("orchestrator.router._call_structured_with_retry", _fake_structured_retry)
    orchestrator = Orchestrator(_config(tmp_path))

    result = await orchestrator.ask(
        "Summarize the latest security news headlines",
        mode="single",
        tools="use web retrieval only",
    )
    assert result.mode == "single"
    assert result.tool_outputs == []
    assert result.warnings is not None
    assert any("intent drift detected" in warning for warning in result.warnings)
