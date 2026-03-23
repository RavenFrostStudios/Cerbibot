from __future__ import annotations

import json
from dataclasses import dataclass

from orchestrator.budgets import BudgetTracker
from orchestrator.collaboration.debate import _is_low_signal_debate_argument, run_debate_workflow
from orchestrator.config import BudgetConfig, SecurityConfig
from orchestrator.providers.base import CompletionResult, ProviderAdapter
from orchestrator.security.guardian import Guardian


@dataclass
class FakePricing:
    input: float = 1.0
    output: float = 1.0


@dataclass
class FakeProviderCfg:
    pricing_usd_per_1m_tokens: dict


class FakeProvider(ProviderAdapter):
    def __init__(self, name: str):
        super().__init__(name, FakeProviderCfg(pricing_usd_per_1m_tokens={"m": FakePricing()}))

    async def complete(self, prompt: str, model: str, max_tokens: int, temperature: float) -> CompletionResult:
        return CompletionResult("ok", 10, 10, model, 1, 0.01, self.provider_name)

    async def complete_structured(self, prompt: str, model: str, output_schema: dict, max_tokens: int, temperature: float) -> CompletionResult:
        payload = json.dumps({k: ("A" if k == "winner" else "value") for k in output_schema.keys()})
        return CompletionResult(payload, 10, 10, model, 1, 0.01, self.provider_name)

    async def complete_stream(self, prompt: str, model: str, max_tokens: int, temperature: float):
        yield "value"


class EmptyFinalSynthProvider(FakeProvider):
    async def complete_structured(self, prompt: str, model: str, output_schema: dict, max_tokens: int, temperature: float) -> CompletionResult:
        if "final_answer" in output_schema:
            return CompletionResult('{"final_answer": ""}', 10, 10, model, 1, 0.01, self.provider_name)
        return await super().complete_structured(prompt, model, output_schema, max_tokens, temperature)


class WeakDebaterBProvider(FakeProvider):
    async def complete_structured(self, prompt: str, model: str, output_schema: dict, max_tokens: int, temperature: float) -> CompletionResult:
        if "argument" in output_schema:
            # Valid JSON shape but semantically weak output.
            return CompletionResult(
                json.dumps({"argument": "N/A", "key_points": []}),
                10,
                10,
                model,
                1,
                0.01,
                self.provider_name,
            )
        return await super().complete_structured(prompt, model, output_schema, max_tokens, temperature)


async def test_debate_workflow(tmp_path) -> None:
    guardian = Guardian(
        SecurityConfig(
            block_on_secrets=True,
            redact_logs=True,
            tool_allowlist=[],
            retrieval_domain_allowlist=[],
            retrieval_domain_denylist=[],
        )
    )
    budgets = BudgetTracker(
        BudgetConfig(
            session_usd_cap=5.0,
            daily_usd_cap=5.0,
            monthly_usd_cap=5.0,
            usage_file=str(tmp_path / "usage.json"),
        )
    )
    result = await run_debate_workflow(
        query="monolith vs microservices",
        debater_a=FakeProvider("openai"),
        debater_b=FakeProvider("anthropic"),
        judge=FakeProvider("openai"),
        synthesizer=FakeProvider("openai"),
        model_a="m",
        model_b="m",
        judge_model="m",
        synth_model="m",
        guardian=guardian,
        budgets=budgets,
    )
    assert result.final_answer
    assert result.judge_winner in {"A", "B", "tie", "value"}
    assert result.total_cost > 0


async def test_debate_workflow_empty_final_answer_uses_deterministic_fallback(tmp_path) -> None:
    guardian = Guardian(
        SecurityConfig(
            block_on_secrets=True,
            redact_logs=True,
            tool_allowlist=[],
            retrieval_domain_allowlist=[],
            retrieval_domain_denylist=[],
        )
    )
    budgets = BudgetTracker(
        BudgetConfig(
            session_usd_cap=5.0,
            daily_usd_cap=5.0,
            monthly_usd_cap=5.0,
            usage_file=str(tmp_path / "usage.json"),
        )
    )
    result = await run_debate_workflow(
        query="bff vs direct microservices",
        debater_a=FakeProvider("openai"),
        debater_b=FakeProvider("anthropic"),
        judge=FakeProvider("openai"),
        synthesizer=EmptyFinalSynthProvider("openai"),
        model_a="m",
        model_b="m",
        judge_model="m",
        synth_model="m",
        guardian=guardian,
        budgets=budgets,
    )
    assert result.final_answer
    assert result.final_answer != '{"final_answer": ""}'
    assert any("empty final answer" in warning.lower() for warning in result.warnings)


async def test_debate_workflow_weak_debater_b_uses_synthetic_counter(tmp_path) -> None:
    guardian = Guardian(
        SecurityConfig(
            block_on_secrets=True,
            redact_logs=True,
            tool_allowlist=[],
            retrieval_domain_allowlist=[],
            retrieval_domain_denylist=[],
        )
    )
    budgets = BudgetTracker(
        BudgetConfig(
            session_usd_cap=5.0,
            daily_usd_cap=5.0,
            monthly_usd_cap=5.0,
            usage_file=str(tmp_path / "usage.json"),
        )
    )
    result = await run_debate_workflow(
        query="bff vs direct microservices",
        debater_a=FakeProvider("openai"),
        debater_b=WeakDebaterBProvider("anthropic"),
        judge=FakeProvider("openai"),
        synthesizer=FakeProvider("openai"),
        model_a="m",
        model_b="m",
        judge_model="m",
        synth_model="m",
        guardian=guardian,
        budgets=budgets,
    )
    assert "Counter-argument to the opposing case" in result.argument_b
    assert any("Debater B remained low-signal" in warning for warning in result.warnings)


def test_instruction_echo_marked_low_signal() -> None:
    low_signal, reasons = _is_low_signal_debate_argument(
        role="debater_a",
        argument="Output only strict valid JSON with required keys argument and key_points.",
        key_points=["No markdown", "Use exact template"],
    )
    assert low_signal is True
    assert "instruction_echo" in reasons


def test_debater_b_first_pass_opposition_is_soft() -> None:
    low_signal, reasons = _is_low_signal_debate_argument(
        role="debater_b",
        argument="This approach has tradeoffs in cost, latency, and reliability and needs a phased rollout plan.",
        key_points=["Operational burden matters", "Phased migration reduces risk"],
        opponent_argument="Use a strongly coupled synchronous baseline immediately.",
        require_opposition_engagement=False,
    )
    assert "weak_opposition_engagement" not in reasons
