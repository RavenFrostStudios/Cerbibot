from __future__ import annotations

import pytest

from orchestrator.collaboration.fact_checker import classify_claim, fallback_extract_claims, run_fact_check
from orchestrator.providers.base import CompletionResult


class FakeAdapter:
    def count_tokens(self, text: str, model: str) -> int:
        return 1

    def estimate_cost(self, tokens_in: int, tokens_out: int, model: str) -> float:
        return 0.0

    async def complete(self, prompt: str, model: str, max_tokens: int, temperature: float) -> CompletionResult:
        return CompletionResult("", 1, 1, model, 1, 0.0, "fake")

    async def complete_structured(self, prompt: str, model: str, output_schema: dict, max_tokens: int, temperature: float) -> CompletionResult:
        return CompletionResult('{"claims": ["Latest Python version is 3.13"]}', 1, 1, model, 1, 0.0, "fake")

    async def complete_stream(self, prompt: str, model: str, max_tokens: int, temperature: float):
        yield ""


def test_classify_claim() -> None:
    assert classify_claim("Latest Python version is 3.13") == "time_sensitive"
    assert classify_claim("Python was created in 1991") == "slow_changing"
    assert classify_claim("Two plus two equals four") == "timeless"


def test_fallback_extract_claims() -> None:
    claims = fallback_extract_claims("A. B? C!")
    assert claims == ["A.", "B?", "C!"]


@pytest.mark.asyncio
async def test_run_fact_check_time_sensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_search(*_args, **_kwargs):
        from orchestrator.retrieval.search import SearchResult

        return [SearchResult(title="Doc", url="https://example.com/doc", snippet="Python 3.13")]

    async def _fake_fetch(url: str, **_kwargs):
        from orchestrator.retrieval.fetch import RetrievedDocument
        from orchestrator.security.taint import TaintedString

        return RetrievedDocument(
            url=url,
            title="Doc",
            retrieved_at="2026-02-10T00:00:00+00:00",
            text=TaintedString(
                value="UNTRUSTED_SOURCE_BEGIN Python version 3.13 UNTRUSTED_SOURCE_END",
                source="retrieved_text",
                source_id=url,
                taint_level="untrusted",
            ),
        )

    monkeypatch.setattr("orchestrator.collaboration.fact_checker.search_web", _fake_search)
    monkeypatch.setattr("orchestrator.collaboration.fact_checker.fetch_url_content", _fake_fetch)

    verified = await run_fact_check(answer_text="x", adapter=FakeAdapter(), model="m")
    assert len(verified) == 1
    assert verified[0].classification == "time_sensitive"
    assert verified[0].verified is True
