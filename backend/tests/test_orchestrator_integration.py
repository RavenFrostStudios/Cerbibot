from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.budgets import BudgetExceededError
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
    def __init__(self, provider_name: str, fail_structured: bool = False):
        self.provider_name = provider_name
        self.fail_structured = fail_structured
        self.last_prompt = ""
        self.config = ProviderConfig(
            enabled=True,
            api_key_env=f"{provider_name.upper()}_API_KEY",
            models=ProviderModelConfig(fast="m", deep="m"),
            pricing_usd_per_1m_tokens={"m": ProviderPricing(input=1.0, output=2.0)},
        )

    def count_tokens(self, text: str, model: str) -> int:
        return max(1, len(text.split()))

    def estimate_cost(self, tokens_in: int, tokens_out: int, model: str) -> float:
        return 0.01

    async def complete(self, prompt: str, model: str, max_tokens: int, temperature: float) -> CompletionResult:
        self.last_prompt = prompt
        return CompletionResult(
            text="single-answer",
            tokens_in=10,
            tokens_out=12,
            model=model,
            latency_ms=1,
            estimated_cost=0.01,
            provider=self.provider_name,
        )

    async def complete_structured(
        self,
        prompt: str,
        model: str,
        output_schema: dict,
        max_tokens: int,
        temperature: float,
    ) -> CompletionResult:
        self.last_prompt = prompt
        if self.fail_structured:
            raise RuntimeError("forced structured failure")
        payload = json.dumps({key: "value" for key in output_schema.keys()})
        return CompletionResult(
            text=payload,
            tokens_in=9,
            tokens_out=9,
            model=model,
            latency_ms=1,
            estimated_cost=0.01,
            provider=self.provider_name,
        )


def _make_mixed_openai_google_config(usage_file: str) -> AppConfig:
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
        "google": ProviderConfig(
            enabled=True,
            api_key_env="GOOGLE_API_KEY",
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
            usage_file=usage_file,
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
                critic_provider="anthropic",
                refiner_provider="openai",
            )
        ),
        retrieval=RetrievalConfig(
            search_provider="duckduckgo_html",
            max_results=5,
            max_fetch_bytes=200_000,
            timeout_seconds=10.0,
        ),
    )


def _make_config(usage_file: str, session_cap: float = 5.0) -> AppConfig:
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
            session_usd_cap=session_cap,
            daily_usd_cap=5.0,
            monthly_usd_cap=5.0,
            usage_file=usage_file,
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
                critic_provider="anthropic",
                refiner_provider="openai",
            )
        ),
        retrieval=RetrievalConfig(
            search_provider="duckduckgo_html",
            max_results=5,
            max_fetch_bytes=200_000,
            timeout_seconds=10.0,
        ),
    )


def _make_google_only_config(usage_file: str) -> AppConfig:
    providers = {
        "openai": ProviderConfig(
            enabled=False,
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
        "google": ProviderConfig(
            enabled=True,
            api_key_env="GOOGLE_API_KEY",
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
            usage_file=usage_file,
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
                critic_provider="anthropic",
                refiner_provider="openai",
            )
        ),
        retrieval=RetrievalConfig(
            search_provider="duckduckgo_html",
            max_results=5,
            max_fetch_bytes=200_000,
            timeout_seconds=10.0,
        ),
    )


def _make_local_only_config(usage_file: str) -> AppConfig:
    providers = {
        "local": ProviderConfig(
            enabled=True,
            api_key_env="LOCAL_API_KEY",
            models=ProviderModelConfig(fast="m", deep="m"),
            pricing_usd_per_1m_tokens={"m": ProviderPricing(input=0.0, output=0.0)},
        ),
        "openai": ProviderConfig(
            enabled=False,
            api_key_env="OPENAI_API_KEY",
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
            usage_file=usage_file,
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
                drafter_provider="local",
                critic_provider="local",
                refiner_provider="local",
            )
        ),
        retrieval=RetrievalConfig(
            search_provider="duckduckgo_html",
            max_results=5,
            max_fetch_bytes=200_000,
            timeout_seconds=10.0,
        ),
    )


@pytest.mark.asyncio
async def test_orchestrator_single_mode_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setattr("orchestrator.router.OpenAIAdapter", lambda _cfg: FakeAdapter("openai"))
    monkeypatch.setattr("orchestrator.router.AnthropicAdapter", lambda _cfg: FakeAdapter("anthropic"))
    orchestrator = Orchestrator(_make_config(str(tmp_path / "usage.json")))

    result = await orchestrator.ask("hello", mode="single")
    assert result.answer == "single-answer"
    assert result.provider == "openai"
    assert result.warnings == []


@pytest.mark.asyncio
async def test_orchestrator_blocks_guardian_preflight(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setattr("orchestrator.router.OpenAIAdapter", lambda _cfg: FakeAdapter("openai"))
    monkeypatch.setattr("orchestrator.router.AnthropicAdapter", lambda _cfg: FakeAdapter("anthropic"))
    orchestrator = Orchestrator(_make_config(str(tmp_path / "usage.json")))

    with pytest.raises(ValueError, match="Blocked by guardian preflight"):
        await orchestrator.ask("my ssn is 123-45-6789", mode="single")


@pytest.mark.asyncio
async def test_orchestrator_budget_exceeded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setattr("orchestrator.router.OpenAIAdapter", lambda _cfg: FakeAdapter("openai"))
    monkeypatch.setattr("orchestrator.router.AnthropicAdapter", lambda _cfg: FakeAdapter("anthropic"))
    orchestrator = Orchestrator(_make_config(str(tmp_path / "usage.json"), session_cap=0.001))

    with pytest.raises(BudgetExceededError):
        await orchestrator.ask("hello", mode="single")


@pytest.mark.asyncio
async def test_orchestrator_rejects_invalid_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setattr("orchestrator.router.OpenAIAdapter", lambda _cfg: FakeAdapter("openai"))
    monkeypatch.setattr("orchestrator.router.AnthropicAdapter", lambda _cfg: FakeAdapter("anthropic"))
    orchestrator = Orchestrator(_make_config(str(tmp_path / "usage.json")))

    with pytest.raises(ValueError, match="Unsupported mode"):
        await orchestrator.ask("hello", mode="bad-mode")


@pytest.mark.asyncio
async def test_orchestrator_rejects_disabled_provider_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setattr("orchestrator.router.OpenAIAdapter", lambda _cfg: FakeAdapter("openai"))
    monkeypatch.setattr("orchestrator.router.AnthropicAdapter", lambda _cfg: FakeAdapter("anthropic"))
    orchestrator = Orchestrator(_make_config(str(tmp_path / "usage.json")))

    with pytest.raises(ValueError, match="Provider is not enabled"):
        await orchestrator.ask("hello", mode="single", provider="not-configured")


@pytest.mark.asyncio
async def test_orchestrator_masks_pii_before_cloud_provider_calls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    openai = FakeAdapter("openai")
    monkeypatch.setattr("orchestrator.router.OpenAIAdapter", lambda _cfg: openai)
    monkeypatch.setattr("orchestrator.router.AnthropicAdapter", lambda _cfg: FakeAdapter("anthropic"))
    orchestrator = Orchestrator(_make_config(str(tmp_path / "usage.json")))

    result = await orchestrator.ask(
        "Contact me at alice@example.com or +1 (555) 123-4567",
        mode="single",
        provider="openai",
    )
    assert result.answer
    assert "alice@example.com" not in openai.last_prompt
    assert "555" not in openai.last_prompt
    assert "[MASK_EMAIL_1]" in openai.last_prompt
    assert "[MASK_PHONE_1]" in openai.last_prompt
    assert result.warnings is not None
    assert any("Privacy masking applied for cloud call" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_orchestrator_keeps_prompt_unmasked_for_local_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LOCAL_API_KEY", "x")
    local = FakeAdapter("local")
    monkeypatch.setattr("orchestrator.router.LocalAdapter", lambda _cfg: local)
    orchestrator = Orchestrator(_make_local_only_config(str(tmp_path / "usage.json")))

    query = "Contact me at alice@example.com"
    result = await orchestrator.ask(query, mode="single", provider="local")
    assert result.answer
    assert "alice@example.com" in local.last_prompt
    assert result.warnings is not None
    assert all("Privacy masking applied for cloud call" not in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_orchestrator_rehydrates_mask_tokens_for_cloud_output_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class EchoMaskAdapter(FakeAdapter):
        async def complete(self, prompt: str, model: str, max_tokens: int, temperature: float) -> CompletionResult:
            self.last_prompt = prompt
            return CompletionResult(
                text="Confirmed contact [MASK_EMAIL_1].",
                tokens_in=10,
                tokens_out=12,
                model=model,
                latency_ms=1,
                estimated_cost=0.01,
                provider=self.provider_name,
            )

    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.delenv("MMO_PRIVACY_REHYDRATE", raising=False)
    adapter = EchoMaskAdapter("openai")
    monkeypatch.setattr("orchestrator.router.OpenAIAdapter", lambda _cfg: adapter)
    monkeypatch.setattr("orchestrator.router.AnthropicAdapter", lambda _cfg: FakeAdapter("anthropic"))
    orchestrator = Orchestrator(_make_config(str(tmp_path / "usage.json")))

    result = await orchestrator.ask("Reach me at alice@example.com", mode="single", provider="openai")

    assert "[MASK_EMAIL_1]" in adapter.last_prompt
    assert "[MASK_EMAIL_1]" not in result.answer
    assert "[REDACTED_PII_EMAIL]" in result.answer
    assert result.warnings is not None
    assert any("Privacy rehydration applied in trusted runtime path." in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_orchestrator_can_disable_rehydration_for_cloud_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class EchoMaskAdapter(FakeAdapter):
        async def complete(self, prompt: str, model: str, max_tokens: int, temperature: float) -> CompletionResult:
            self.last_prompt = prompt
            return CompletionResult(
                text="Confirmed contact [MASK_EMAIL_1].",
                tokens_in=10,
                tokens_out=12,
                model=model,
                latency_ms=1,
                estimated_cost=0.01,
                provider=self.provider_name,
            )

    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("MMO_PRIVACY_REHYDRATE", "0")
    adapter = EchoMaskAdapter("openai")
    monkeypatch.setattr("orchestrator.router.OpenAIAdapter", lambda _cfg: adapter)
    monkeypatch.setattr("orchestrator.router.AnthropicAdapter", lambda _cfg: FakeAdapter("anthropic"))
    orchestrator = Orchestrator(_make_config(str(tmp_path / "usage.json")))

    result = await orchestrator.ask("Reach me at alice@example.com", mode="single", provider="openai")

    assert "[MASK_EMAIL_1]" in adapter.last_prompt
    assert "[MASK_EMAIL_1]" in result.answer
    assert "alice@example.com" not in result.answer


def test_orchestrator_role_routing_apply_and_validate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setattr("orchestrator.router.OpenAIAdapter", lambda _cfg: FakeAdapter("openai"))
    monkeypatch.setattr("orchestrator.router.AnthropicAdapter", lambda _cfg: FakeAdapter("anthropic"))
    orchestrator = Orchestrator(_make_config(str(tmp_path / "usage.json")))

    applied = orchestrator.apply_role_routes(
        {
            "critique": {
                "drafter_provider": "openai",
                "critic_provider": "anthropic",
                "refiner_provider": "openai",
            },
            "debate": {
                "debater_a_provider": "openai",
                "debater_b_provider": "anthropic",
                "judge_provider": "openai",
                "synthesizer_provider": "openai",
            },
            "consensus": {"adjudicator_provider": "openai"},
            "council": {
                "specialist_roles": {"coding": "openai", "security": "anthropic", "writing": "", "factual": ""},
                "synthesizer_provider": "openai",
            },
        }
    )
    assert applied["debate"]["debater_b_provider"] == "anthropic"
    assert orchestrator.get_role_routes()["council"]["synthesizer_provider"] == "openai"

    with pytest.raises(ValueError, match="unknown provider"):
        orchestrator.apply_role_routes(
            {
                "critique": {
                    "drafter_provider": "missing",
                    "critic_provider": "anthropic",
                    "refiner_provider": "openai",
                }
            }
        )


def test_orchestrator_provider_overrides_reconciles_routes_when_disabling_routed_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setattr("orchestrator.router.OpenAIAdapter", lambda _cfg: FakeAdapter("openai"))
    monkeypatch.setattr("orchestrator.router.AnthropicAdapter", lambda _cfg: FakeAdapter("anthropic"))
    orchestrator = Orchestrator(_make_config(str(tmp_path / "usage.json")))

    orchestrator.apply_role_routes(
        {
            "critique": {
                "drafter_provider": "openai",
                "critic_provider": "anthropic",
                "refiner_provider": "openai",
            }
        }
    )
    result = orchestrator.apply_provider_overrides([{"name": "anthropic", "enabled": False}])
    assert result["updated"]
    routes = orchestrator.get_role_routes()
    assert routes["critique"]["critic_provider"] == "openai"


@pytest.mark.asyncio
async def test_orchestrator_critique_fallback_warning(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MMO_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setattr("orchestrator.router.OpenAIAdapter", lambda _cfg: FakeAdapter("openai"))
    monkeypatch.setattr("orchestrator.router.AnthropicAdapter", lambda _cfg: FakeAdapter("anthropic", fail_structured=True))
    orchestrator = Orchestrator(_make_config(str(tmp_path / "usage.json")))

    result = await orchestrator.ask("explain CAP theorem", mode="critique")
    assert result.mode == "critique"
    assert result.warnings is not None
    assert any("Critique step failed" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_orchestrator_collab_modes_work_with_only_google_enabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MMO_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GOOGLE_API_KEY", "x")
    monkeypatch.setattr("orchestrator.router.GoogleAdapter", lambda _cfg: FakeAdapter("google"))
    orchestrator = Orchestrator(_make_google_only_config(str(tmp_path / "usage.json")))

    critique = await orchestrator.ask("summarize this", mode="critique")
    debate = await orchestrator.ask("summarize this", mode="debate")
    council = await orchestrator.ask("summarize this", mode="council")

    assert critique.answer
    assert debate.answer
    assert council.answer
    assert critique.mode == "critique"
    assert debate.mode == "debate"
    assert council.mode == "council"


@pytest.mark.asyncio
async def test_orchestrator_collab_modes_work_with_mixed_provider_availability(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MMO_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("GOOGLE_API_KEY", "x")
    monkeypatch.setattr("orchestrator.router.OpenAIAdapter", lambda _cfg: FakeAdapter("openai"))
    monkeypatch.setattr("orchestrator.router.GoogleAdapter", lambda _cfg: FakeAdapter("google"))
    orchestrator = Orchestrator(_make_mixed_openai_google_config(str(tmp_path / "usage.json")))

    critique = await orchestrator.ask("summarize this", mode="critique")
    debate = await orchestrator.ask("summarize this", mode="debate")
    consensus = await orchestrator.ask("summarize this", mode="consensus")
    council = await orchestrator.ask("summarize this", mode="council")

    assert critique.answer
    assert debate.answer
    assert consensus.answer
    assert council.answer
    assert critique.mode == "critique"
    assert debate.mode == "debate"
    assert consensus.mode == "consensus"
    assert council.mode == "council"
    assert critique.warnings is not None
    assert any("critic provider 'anthropic' unavailable" in warning for warning in critique.warnings)


@pytest.mark.asyncio
async def test_orchestrator_collab_modes_emit_shared_state_in_verbose_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MMO_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setattr("orchestrator.router.OpenAIAdapter", lambda _cfg: FakeAdapter("openai"))
    monkeypatch.setattr("orchestrator.router.AnthropicAdapter", lambda _cfg: FakeAdapter("anthropic"))
    orchestrator = Orchestrator(_make_config(str(tmp_path / "usage.json")))

    critique = await orchestrator.ask("state test", mode="critique", verbose=True)
    debate = await orchestrator.ask("state test", mode="debate", verbose=True)
    consensus = await orchestrator.ask("state test", mode="consensus", verbose=True)
    council = await orchestrator.ask("state test", mode="council", verbose=True)

    assert critique.shared_state is not None
    assert debate.shared_state is not None
    assert consensus.shared_state is not None
    assert council.shared_state is not None
    assert critique.shared_state.get("version") == "mmy-shared-state-v1"
    assert debate.shared_state.get("mode") == "debate"


@pytest.mark.asyncio
async def test_orchestrator_collab_modes_emit_compact_shared_state_without_verbose(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MMO_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setattr("orchestrator.router.OpenAIAdapter", lambda _cfg: FakeAdapter("openai"))
    monkeypatch.setattr("orchestrator.router.AnthropicAdapter", lambda _cfg: FakeAdapter("anthropic"))
    orchestrator = Orchestrator(_make_config(str(tmp_path / "usage.json")))

    critique = await orchestrator.ask("state test", mode="critique", verbose=False)
    debate = await orchestrator.ask("state test", mode="debate", verbose=False)
    consensus = await orchestrator.ask("state test", mode="consensus", verbose=False)
    council = await orchestrator.ask("state test", mode="council", verbose=False)

    assert critique.shared_state is not None
    assert debate.shared_state is not None
    assert consensus.shared_state is not None
    assert council.shared_state is not None
    assert critique.shared_state.get("version") == "mmy-shared-state-v1"
    assert critique.shared_state.get("mode") == "critique"
    critique_stages = critique.shared_state.get("stages")
    assert isinstance(critique_stages, list)
    assert critique_stages
    first_stage = critique_stages[0]
    assert isinstance(first_stage, dict)
    assert "output" not in first_stage


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "error_text", "expected_warning"),
    [
        ("critique", "429 RESOURCE_EXHAUSTED", "Critique mode fallback to single provider path."),
        ("critique", "timeout while waiting", "Critique mode fallback to single provider path."),
        ("debate", "429 RESOURCE_EXHAUSTED", "Debate mode fallback to single provider path."),
        ("debate", "timeout while waiting", "Debate mode fallback to single provider path."),
        ("consensus", "429 RESOURCE_EXHAUSTED", "Consensus mode fallback to single provider path."),
        ("consensus", "timeout while waiting", "Consensus mode fallback to single provider path."),
        ("council", "429 RESOURCE_EXHAUSTED", "Council mode fallback to single provider path."),
        ("council", "timeout while waiting", "Council mode fallback to single provider path."),
    ],
)
async def test_orchestrator_collab_modes_fallback_on_quota_or_timeout_style_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mode: str,
    error_text: str,
    expected_warning: str,
) -> None:
    monkeypatch.setenv("MMO_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setattr("orchestrator.router.OpenAIAdapter", lambda _cfg: FakeAdapter("openai"))
    monkeypatch.setattr("orchestrator.router.AnthropicAdapter", lambda _cfg: FakeAdapter("anthropic"))

    async def _raise_workflow_error(**_kwargs):
        raise RuntimeError(error_text)

    if mode == "critique":
        monkeypatch.setattr("orchestrator.router.run_workflow", _raise_workflow_error)
    elif mode == "debate":
        monkeypatch.setattr("orchestrator.router.run_debate_workflow", _raise_workflow_error)
    elif mode == "consensus":
        monkeypatch.setattr("orchestrator.router.run_consensus_workflow", _raise_workflow_error)
    elif mode == "council":
        monkeypatch.setattr("orchestrator.router.run_council_workflow", _raise_workflow_error)
    else:
        raise AssertionError(f"Unhandled mode in test: {mode}")

    orchestrator = Orchestrator(_make_config(str(tmp_path / "usage.json")))
    result = await orchestrator.ask("resilience check", mode=mode)

    assert result.mode == mode
    assert result.answer
    assert result.warnings is not None
    assert any(expected_warning in warning for warning in result.warnings)
    assert any(error_text in warning for warning in result.warnings)
