from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from orchestrator.budgets import BudgetTracker
from orchestrator.collaboration.council import run_council_workflow
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
        if "final_answer" in output_schema:
            payload = json.dumps({"final_answer": "council final", "notes": "synthesis notes"})
        else:
            payload = json.dumps(
                {
                    "answer": "specialist answer",
                    "claims": ["claim 1"],
                    "assumptions": ["assumption 1"],
                    "evidence_needed": ["evidence 1"],
                }
            )
        return CompletionResult(payload, 12, 12, model, 1, 0.01, self.provider_name)

    async def complete_stream(self, prompt: str, model: str, max_tokens: int, temperature: float):
        yield "ok"


@pytest.mark.asyncio
async def test_council_workflow(tmp_path) -> None:
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
    result = await run_council_workflow(
        query="design auth system",
        specialists=[
            ("coding", FakeProvider("openai"), "m"),
            ("security", FakeProvider("anthropic"), "m"),
        ],
        synthesizer=FakeProvider("openai"),
        synthesizer_model="m",
        guardian=guardian,
        budgets=budgets,
    )
    assert result.final_answer == "council final"
    assert len(result.specialists) == 2
    assert result.total_cost > 0
