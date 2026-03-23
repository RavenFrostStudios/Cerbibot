from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from orchestrator.budgets import BudgetTracker
from orchestrator.collaboration.consensus import run_consensus_workflow
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
    def __init__(self, name: str, answer: str):
        super().__init__(name, FakeProviderCfg(pricing_usd_per_1m_tokens={"m": FakePricing()}))
        self.answer = answer

    async def complete(self, prompt: str, model: str, max_tokens: int, temperature: float) -> CompletionResult:
        return CompletionResult(self.answer, 10, 10, model, 1, 0.01, self.provider_name)

    async def complete_structured(
        self,
        prompt: str,
        model: str,
        output_schema: dict,
        max_tokens: int,
        temperature: float,
    ) -> CompletionResult:
        payload = json.dumps({"final_answer": "Canberra", "confidence": 0.91, "reason": "sources align"})
        return CompletionResult(payload, 12, 10, model, 1, 0.01, self.provider_name)

    async def complete_stream(self, prompt: str, model: str, max_tokens: int, temperature: float):
        yield self.answer


@pytest.mark.asyncio
async def test_consensus_high_agreement(tmp_path) -> None:
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

    result = await run_consensus_workflow(
        query="What is the capital of Australia?",
        participants={
            "openai": (FakeProvider("openai", "The capital of Australia is Canberra."), "m"),
            "anthropic": (FakeProvider("anthropic", "Canberra is the capital city of Australia."), "m"),
        },
        adjudicator=FakeProvider("openai", "unused"),
        adjudicator_model="m",
        guardian=guardian,
        budgets=budgets,
        retrieval_search_provider="duckduckgo_html",
        retrieval_max_results=2,
        retrieval_timeout_seconds=1.0,
        retrieval_max_fetch_bytes=5000,
        retrieval_domain_allowlist=[],
        retrieval_domain_denylist=[],
    )
    assert result.used_adjudication is False
    assert "Canberra" in result.final_answer
    assert result.confidence >= 0.55


@pytest.mark.asyncio
async def test_consensus_low_agreement_triggers_adjudication(monkeypatch, tmp_path) -> None:
    guardian = Guardian(
        SecurityConfig(
            block_on_secrets=True,
            redact_logs=True,
            tool_allowlist=[],
            retrieval_domain_allowlist=["example.com"],
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

    async def _fake_search(*_args, **_kwargs):
        from orchestrator.retrieval.search import SearchResult

        return [SearchResult(title="Doc", url="https://example.com/doc", snippet="snippet")]

    async def _fake_fetch(url: str, **_kwargs):
        from orchestrator.retrieval.fetch import RetrievedDocument
        from orchestrator.security.taint import TaintedString

        return RetrievedDocument(
            url=url,
            title="Doc",
            retrieved_at="2026-02-10T00:00:00+00:00",
            text=TaintedString(
                value="UNTRUSTED_SOURCE_BEGIN\\nCanberra is Australia's capital\\nUNTRUSTED_SOURCE_END",
                source="retrieved_text",
                source_id=url,
                taint_level="untrusted",
            ),
        )

    monkeypatch.setattr("orchestrator.collaboration.consensus.search_web", _fake_search)
    monkeypatch.setattr("orchestrator.collaboration.consensus.fetch_url_content", _fake_fetch)

    result = await run_consensus_workflow(
        query="What is the capital of Australia?",
        participants={
            "openai": (FakeProvider("openai", "The capital is Sydney."), "m"),
            "anthropic": (FakeProvider("anthropic", "It is Melbourne."), "m"),
        },
        adjudicator=FakeProvider("openai", "unused"),
        adjudicator_model="m",
        guardian=guardian,
        budgets=budgets,
        retrieval_search_provider="duckduckgo_html",
        retrieval_max_results=2,
        retrieval_timeout_seconds=1.0,
        retrieval_max_fetch_bytes=5000,
        retrieval_domain_allowlist=["example.com"],
        retrieval_domain_denylist=[],
    )
    assert result.used_adjudication is True
    assert result.adjudication_reason is not None
    assert result.final_answer == "Canberra"
    assert len(result.citations) == 1
