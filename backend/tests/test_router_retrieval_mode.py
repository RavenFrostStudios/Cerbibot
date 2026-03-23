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
        self.last_prompt = ""

    def count_tokens(self, text: str, model: str) -> int:
        return 5

    def estimate_cost(self, tokens_in: int, tokens_out: int, model: str) -> float:
        return 0.01

    async def complete(self, prompt: str, model: str, max_tokens: int, temperature: float) -> CompletionResult:
        self.last_prompt = prompt
        return CompletionResult(
            text="grounded answer",
            tokens_in=10,
            tokens_out=12,
            model=model,
            latency_ms=1,
            estimated_cost=0.01,
            provider=self.provider_name,
        )

    async def complete_structured(self, prompt: str, model: str, output_schema: dict, max_tokens: int, temperature: float) -> CompletionResult:
        return CompletionResult("{}", 1, 1, model, 1, 0.01, self.provider_name)

    async def complete_stream(self, prompt: str, model: str, max_tokens: int, temperature: float):
        yield "grounded "
        yield "answer"


class PlaceholderCitationAdapter(FakeAdapter):
    async def complete(self, prompt: str, model: str, max_tokens: int, temperature: float) -> CompletionResult:
        self.last_prompt = prompt
        return CompletionResult(
            text="[1][2][3]",
            tokens_in=10,
            tokens_out=12,
            model=model,
            latency_ms=1,
            estimated_cost=0.01,
            provider=self.provider_name,
        )


def test_normalize_inline_citation_order() -> None:
    text = "Verified by [1] and [3], [2]. Another group [2][1][2]."
    normalized = Orchestrator._normalize_inline_citation_order(text)
    assert normalized == "Verified by [1] and [2][3]. Another group [1][2]."


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
async def test_retrieval_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "y")
    monkeypatch.setattr("orchestrator.router.OpenAIAdapter", lambda _cfg: FakeAdapter("openai"))
    monkeypatch.setattr("orchestrator.router.AnthropicAdapter", lambda _cfg: FakeAdapter("anthropic"))

    async def _fake_search(*_args, **_kwargs):
        from orchestrator.retrieval.search import SearchResult

        return [SearchResult(title="Doc", url="https://example.com/doc", snippet="Snippet")]

    async def _fake_fetch(url: str, **_kwargs):
        from orchestrator.retrieval.fetch import RetrievedDocument
        from orchestrator.security.taint import TaintedString

        return RetrievedDocument(
            url=url,
            title="Doc",
            retrieved_at="2026-02-10T00:00:00+00:00",
            text=TaintedString(
                value="UNTRUSTED_SOURCE_BEGIN\\nPython info\\nUNTRUSTED_SOURCE_END",
                source="retrieved_text",
                source_id=url,
                taint_level="untrusted",
            ),
        )

    monkeypatch.setattr("orchestrator.router.search_web", _fake_search)
    monkeypatch.setattr("orchestrator.router.fetch_url_content", _fake_fetch)

    orchestrator = Orchestrator(_config(tmp_path))
    result = await orchestrator.ask("latest python", mode="retrieval")
    assert result.mode == "retrieval"
    assert result.answer == "grounded answer"
    assert result.citations is not None
    assert any(c.url == "https://example.com/doc" for c in result.citations)
    assert result.shared_state is not None
    assert result.shared_state.get("mode") == "retrieval"
    summary = result.shared_state.get("summary")
    assert isinstance(summary, dict)
    assert summary.get("status") == "grounded"
    assert int(summary.get("citations_count", 0)) >= 1


@pytest.mark.asyncio
async def test_retrieval_mode_uses_local_code_index_for_coding_queries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "y")
    monkeypatch.setenv("MMO_WORKSPACE_ROOT", str(tmp_path))
    (tmp_path / "app.py").write_text("def parse_event(payload):\n    return payload.get('id')\n", encoding="utf-8")

    openai_adapter = FakeAdapter("openai")
    monkeypatch.setattr("orchestrator.router.OpenAIAdapter", lambda _cfg: openai_adapter)
    monkeypatch.setattr("orchestrator.router.AnthropicAdapter", lambda _cfg: FakeAdapter("anthropic"))

    async def _no_web_search(*_args, **_kwargs):
        return []

    monkeypatch.setattr("orchestrator.router.search_web", _no_web_search)

    orchestrator = Orchestrator(_config(tmp_path))
    result = await orchestrator.ask("python parse_event function", mode="retrieval")

    assert result.mode == "retrieval"
    assert result.citations is not None
    assert any(c.url.startswith("file://") for c in result.citations)
    assert "file://" in openai_adapter.last_prompt
    assert result.shared_state is not None
    stages = result.shared_state.get("stages")
    assert isinstance(stages, list)
    local_stage = next((stage for stage in stages if isinstance(stage, dict) and stage.get("name") == "local_code_index"), None)
    assert isinstance(local_stage, dict)
    metadata = local_stage.get("metadata")
    assert isinstance(metadata, dict)
    assert int(metadata.get("matched_files", 0)) >= 1


@pytest.mark.asyncio
async def test_retrieval_mode_returns_safe_fallback_when_no_sources(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "y")
    monkeypatch.setattr("orchestrator.router.OpenAIAdapter", lambda _cfg: FakeAdapter("openai"))
    monkeypatch.setattr("orchestrator.router.AnthropicAdapter", lambda _cfg: FakeAdapter("anthropic"))

    async def _no_web_search(*_args, **_kwargs):
        return []

    monkeypatch.setattr("orchestrator.router.search_web", _no_web_search)

    orchestrator = Orchestrator(_config(tmp_path))
    result = await orchestrator.ask("weather in montreal today", mode="retrieval")

    assert result.mode == "retrieval"
    assert result.provider == "none"
    assert result.citations == []
    assert "cannot provide a grounded web answer" in result.answer.lower()
    assert result.warnings is not None
    assert any("No sources retrieved" in warning for warning in result.warnings)
    assert result.shared_state is not None
    summary = result.shared_state.get("summary")
    assert isinstance(summary, dict)
    assert summary.get("status") == "failed"


@pytest.mark.asyncio
async def test_retrieval_mode_time_fallback_returns_grounded_answer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "y")
    monkeypatch.setattr("orchestrator.router.OpenAIAdapter", lambda _cfg: FakeAdapter("openai"))
    monkeypatch.setattr("orchestrator.router.AnthropicAdapter", lambda _cfg: FakeAdapter("anthropic"))

    async def _no_web_search(*_args, **_kwargs):
        return []

    async def _fake_time_doc(*_args, **_kwargs):
        from orchestrator.retrieval.fetch import RetrievedDocument
        from orchestrator.security.taint import TaintedString

        return RetrievedDocument(
            url="https://www.iana.org/time-zones",
            title="Current local time for London",
            retrieved_at="2026-02-18T01:01:00+00:00",
            text=TaintedString(
                value=(
                    "UNTRUSTED_SOURCE_BEGIN\n"
                    "Location: London\n"
                    "Local time: 01:01:02\n"
                    "Local date: Wednesday, February 18, 2026\n"
                    "Timezone: Europe/London (GMT, UTC+00:00)\n"
                    "UNTRUSTED_SOURCE_END"
                ),
                source="retrieved_text",
                source_id="https://www.iana.org/time-zones",
                taint_level="untrusted",
            ),
        )

    monkeypatch.setattr("orchestrator.router.search_web", _no_web_search)
    monkeypatch.setattr("orchestrator.router.fetch_time_document", _fake_time_doc)

    orchestrator = Orchestrator(_config(tmp_path))
    result = await orchestrator.ask("What time is it in London right now?", mode="retrieval")

    assert result.mode == "retrieval"
    assert result.provider == "openai"
    assert result.citations is not None
    assert any("iana.org" in c.url for c in result.citations)
    assert result.shared_state is not None
    summary = result.shared_state.get("summary")
    assert isinstance(summary, dict)
    assert summary.get("status") == "grounded"
    assert bool(summary.get("time_fallback_used")) is True


@pytest.mark.asyncio
async def test_retrieval_mode_time_fallback_preempts_web_search(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "y")
    monkeypatch.setattr("orchestrator.router.OpenAIAdapter", lambda _cfg: FakeAdapter("openai"))
    monkeypatch.setattr("orchestrator.router.AnthropicAdapter", lambda _cfg: FakeAdapter("anthropic"))

    called = {"search": False}

    async def _fake_search(*_args, **_kwargs):
        called["search"] = True
        from orchestrator.retrieval.search import SearchResult

        return [SearchResult(title="Doc", url="https://example.com/doc", snippet="Snippet")]

    async def _fake_time_doc(*_args, **_kwargs):
        from orchestrator.retrieval.fetch import RetrievedDocument
        from orchestrator.security.taint import TaintedString

        return RetrievedDocument(
            url="https://www.iana.org/time-zones",
            title="Current local time for London",
            retrieved_at="2026-02-18T01:01:00+00:00",
            text=TaintedString(
                value=(
                    "UNTRUSTED_SOURCE_BEGIN\n"
                    "Location: London\n"
                    "Local time: 01:01:02\n"
                    "Local date: Wednesday, February 18, 2026\n"
                    "Timezone: Europe/London (GMT, UTC+00:00)\n"
                    "UNTRUSTED_SOURCE_END"
                ),
                source="retrieved_text",
                source_id="https://www.iana.org/time-zones",
                taint_level="untrusted",
            ),
        )

    monkeypatch.setattr("orchestrator.router.search_web", _fake_search)
    monkeypatch.setattr("orchestrator.router.fetch_time_document", _fake_time_doc)

    orchestrator = Orchestrator(_config(tmp_path))
    result = await orchestrator.ask(
        "What time is it in London right now? Include exact date/time and timezone.",
        mode="retrieval",
    )

    assert result.mode == "retrieval"
    assert called["search"] is False
    assert result.shared_state is not None
    summary = result.shared_state.get("summary")
    assert isinstance(summary, dict)
    assert bool(summary.get("time_fallback_used")) is True


@pytest.mark.asyncio
async def test_retrieval_mode_finance_fallback_preempts_web_search(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "y")
    monkeypatch.setattr("orchestrator.router.OpenAIAdapter", lambda _cfg: FakeAdapter("openai"))
    monkeypatch.setattr("orchestrator.router.AnthropicAdapter", lambda _cfg: FakeAdapter("anthropic"))

    called = {"search": False}

    async def _fake_search(*_args, **_kwargs):
        called["search"] = True
        from orchestrator.retrieval.search import SearchResult

        return [SearchResult(title="Doc", url="https://example.com/doc", snippet="Snippet")]

    async def _fake_finance_doc(*_args, **_kwargs):
        from orchestrator.retrieval.fetch import RetrievedDocument
        from orchestrator.security.taint import TaintedString

        return RetrievedDocument(
            url="https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum&vs_currencies=usd",
            title="CoinGecko BTC/ETH spot prices",
            retrieved_at="2026-02-18T01:53:03+00:00",
            text=TaintedString(
                value=(
                    "UNTRUSTED_SOURCE_BEGIN\n"
                    "Bitcoin (BTC) USD: 50000\n"
                    "Ethereum (ETH) USD: 3000\n"
                    "UNTRUSTED_SOURCE_END"
                ),
                source="retrieved_text",
                source_id="https://api.coingecko.com",
                taint_level="untrusted",
            ),
        )

    monkeypatch.setattr("orchestrator.router.search_web", _fake_search)
    monkeypatch.setattr("orchestrator.router.fetch_finance_document", _fake_finance_doc)

    orchestrator = Orchestrator(_config(tmp_path))
    result = await orchestrator.ask(
        "What is the current price of Bitcoin and Ethereum right now? Include source timestamps.",
        mode="retrieval",
    )

    assert result.mode == "retrieval"
    assert called["search"] is False
    assert result.provider == "local"
    assert "Bitcoin (BTC) USD" in result.answer
    assert result.shared_state is not None
    summary = result.shared_state.get("summary")
    assert isinstance(summary, dict)
    assert bool(summary.get("finance_fallback_used")) is True


@pytest.mark.asyncio
async def test_retrieval_mode_treasury_finance_fallback_preempts_web_search(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "y")
    monkeypatch.setattr("orchestrator.router.OpenAIAdapter", lambda _cfg: FakeAdapter("openai"))
    monkeypatch.setattr("orchestrator.router.AnthropicAdapter", lambda _cfg: FakeAdapter("anthropic"))

    called = {"search": False}

    async def _fake_search(*_args, **_kwargs):
        called["search"] = True
        return []

    async def _fake_finance_doc(*_args, **_kwargs):
        from orchestrator.retrieval.fetch import RetrievedDocument
        from orchestrator.security.taint import TaintedString

        return RetrievedDocument(
            url="https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10",
            title="FRED DGS10",
            retrieved_at="2026-02-18T02:18:54+00:00",
            text=TaintedString(
                value=(
                    "UNTRUSTED_SOURCE_BEGIN\n"
                    "US 10Y Treasury yield (%): 4.210\n"
                    "Short trend signal: up (+0.030 vs prior point)\n"
                    "UNTRUSTED_SOURCE_END"
                ),
                source="retrieved_text",
                source_id="https://fred.stlouisfed.org",
                taint_level="untrusted",
            ),
        )

    monkeypatch.setattr("orchestrator.router.search_web", _fake_search)
    monkeypatch.setattr("orchestrator.router.fetch_finance_document", _fake_finance_doc)

    orchestrator = Orchestrator(_config(tmp_path))
    result = await orchestrator.ask(
        "Find current US 10-year treasury yield and explain trend in 2 lines.",
        mode="retrieval",
    )

    assert result.mode == "retrieval"
    assert called["search"] is False
    assert result.provider == "local"
    assert "Current US 10-year Treasury yield" in result.answer
    assert "Trend:" in result.answer
    assert result.shared_state is not None
    summary = result.shared_state.get("summary")
    assert isinstance(summary, dict)
    assert bool(summary.get("finance_fallback_used")) is True


@pytest.mark.asyncio
async def test_retrieval_mode_sports_fallback_preempts_web_search(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "y")
    monkeypatch.setattr("orchestrator.router.OpenAIAdapter", lambda _cfg: FakeAdapter("openai"))
    monkeypatch.setattr("orchestrator.router.AnthropicAdapter", lambda _cfg: FakeAdapter("anthropic"))

    called = {"search": False}

    async def _fake_search(*_args, **_kwargs):
        called["search"] = True
        return []

    async def _fake_sports_doc(*_args, **_kwargs):
        from orchestrator.retrieval.fetch import RetrievedDocument
        from orchestrator.security.taint import TaintedString

        return RetrievedDocument(
            url="https://site.api.espn.com/apis/v2/sports/basketball/nba/standings",
            title="NBA standings",
            retrieved_at="2026-02-18T02:18:54+00:00",
            text=TaintedString(
                value=(
                    "UNTRUSTED_SOURCE_BEGIN\n"
                    "1. Team A — 40-10 (PCT .800)\n"
                    "2. Team B — 39-11 (PCT .780)\n"
                    "UNTRUSTED_SOURCE_END"
                ),
                source="retrieved_text",
                source_id="https://site.api.espn.com",
                taint_level="untrusted",
            ),
        )

    monkeypatch.setattr("orchestrator.router.search_web", _fake_search)
    monkeypatch.setattr("orchestrator.router.fetch_nba_standings_document", _fake_sports_doc)

    orchestrator = Orchestrator(_config(tmp_path))
    result = await orchestrator.ask("What are the latest NBA standings right now?", mode="retrieval")

    assert result.mode == "retrieval"
    assert called["search"] is False
    assert result.provider == "local"
    assert "Team A" in result.answer
    assert result.shared_state is not None
    summary = result.shared_state.get("summary")
    assert isinstance(summary, dict)
    assert bool(summary.get("sports_fallback_used")) is True


@pytest.mark.asyncio
async def test_retrieval_mode_search_uses_clean_user_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "y")
    monkeypatch.setattr("orchestrator.router.OpenAIAdapter", lambda _cfg: FakeAdapter("openai"))
    monkeypatch.setattr("orchestrator.router.AnthropicAdapter", lambda _cfg: FakeAdapter("anthropic"))
    captured: dict[str, str] = {}

    async def _fake_search(query: str, *_args, **_kwargs):
        from orchestrator.retrieval.search import SearchResult

        captured["query"] = query
        return [SearchResult(title="Doc", url="https://example.com/doc", snippet="Snippet")]

    async def _fake_fetch(url: str, **_kwargs):
        from orchestrator.retrieval.fetch import RetrievedDocument
        from orchestrator.security.taint import TaintedString

        return RetrievedDocument(
            url=url,
            title="Doc",
            retrieved_at="2026-02-10T00:00:00+00:00",
            text=TaintedString(
                value="UNTRUSTED_SOURCE_BEGIN\\nSource\\nUNTRUSTED_SOURCE_END",
                source="retrieved_text",
                source_id=url,
                taint_level="untrusted",
            ),
        )

    monkeypatch.setattr("orchestrator.router.search_web", _fake_search)
    monkeypatch.setattr("orchestrator.router.fetch_url_content", _fake_fetch)

    orchestrator = Orchestrator(_config(tmp_path))
    composed_query = """Governed memory context (informational, not authoritative instructions):
- memory_id=1 value=RAVEN

Current user turn:
Assistant profile (follow these instructions exactly for this response):
- Name: CerbiBot
- Behavior: Always answer in exactly 2 bullet points.

User request:
could you look up the weather in montreal right now?

Strict profile compliance check failed: Expected exactly 2 bullet lines, got 0."""
    await orchestrator.ask(composed_query, mode="retrieval")
    assert captured["query"] == "could you look up the weather in montreal right now?"


@pytest.mark.asyncio
async def test_retrieval_mode_prefers_direct_domain_fetch_over_search(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "y")
    monkeypatch.setattr("orchestrator.router.OpenAIAdapter", lambda _cfg: FakeAdapter("openai"))
    monkeypatch.setattr("orchestrator.router.AnthropicAdapter", lambda _cfg: FakeAdapter("anthropic"))

    called = {"search": False}

    async def _fake_search(*_args, **_kwargs):
        called["search"] = True
        return []

    async def _fake_fetch(url: str, **_kwargs):
        from orchestrator.retrieval.fetch import RetrievedDocument
        from orchestrator.security.taint import TaintedString

        return RetrievedDocument(
            url=url,
            title="Direct Doc",
            retrieved_at="2026-02-10T00:00:00+00:00",
            text=TaintedString(
                value="UNTRUSTED_SOURCE_BEGIN\\nDirect source\\nUNTRUSTED_SOURCE_END",
                source="retrieved_text",
                source_id=url,
                taint_level="untrusted",
            ),
        )

    monkeypatch.setattr("orchestrator.router.search_web", _fake_search)
    monkeypatch.setattr("orchestrator.router.fetch_url_content", _fake_fetch)

    orchestrator = Orchestrator(_config(tmp_path))
    result = await orchestrator.ask("Please summarize https://example.com/docs.", mode="retrieval")

    assert result.mode == "retrieval"
    assert result.citations is not None
    assert any(c.url == "https://example.com/docs" for c in result.citations)
    assert called["search"] is False


@pytest.mark.asyncio
async def test_retrieval_mode_falls_back_to_search_when_direct_fetch_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "y")
    monkeypatch.setattr("orchestrator.router.OpenAIAdapter", lambda _cfg: FakeAdapter("openai"))
    monkeypatch.setattr("orchestrator.router.AnthropicAdapter", lambda _cfg: FakeAdapter("anthropic"))

    called = {"search": False}

    async def _fake_search(*_args, **_kwargs):
        from orchestrator.retrieval.search import SearchResult

        called["search"] = True
        return [SearchResult(title="Fallback", url="https://example.com/fallback", snippet="fallback")]

    async def _fake_fetch(url: str, **_kwargs):
        from orchestrator.retrieval.fetch import RetrievedDocument
        from orchestrator.security.taint import TaintedString

        if url == "https://example.com/docs":
            raise RuntimeError("direct fetch failed")
        return RetrievedDocument(
            url=url,
            title="Fallback Doc",
            retrieved_at="2026-02-10T00:00:00+00:00",
            text=TaintedString(
                value="UNTRUSTED_SOURCE_BEGIN\\nFallback source\\nUNTRUSTED_SOURCE_END",
                source="retrieved_text",
                source_id=url,
                taint_level="untrusted",
            ),
        )

    monkeypatch.setattr("orchestrator.router.search_web", _fake_search)
    monkeypatch.setattr("orchestrator.router.fetch_url_content", _fake_fetch)

    orchestrator = Orchestrator(_config(tmp_path))
    result = await orchestrator.ask("Please summarize https://example.com/docs.", mode="retrieval")

    assert result.mode == "retrieval"
    assert result.citations is not None
    assert any(c.url == "https://example.com/fallback" for c in result.citations)
    assert called["search"] is True
    assert not any("Direct URL fetch failed:" in warning for warning in (result.warnings or []))


@pytest.mark.asyncio
async def test_retrieval_mode_returns_search_error_warning_when_all_sources_fail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "y")
    monkeypatch.setattr("orchestrator.router.OpenAIAdapter", lambda _cfg: FakeAdapter("openai"))
    monkeypatch.setattr("orchestrator.router.AnthropicAdapter", lambda _cfg: FakeAdapter("anthropic"))

    async def _failing_search(*_args, **_kwargs):
        raise RuntimeError("duckduckgo search failed: challenge page")

    monkeypatch.setattr("orchestrator.router.search_web", _failing_search)

    orchestrator = Orchestrator(_config(tmp_path))
    result = await orchestrator.ask("please find latest weather for montreal", mode="retrieval")

    assert result.provider == "none"
    assert result.citations == []
    assert result.warnings is not None
    assert any(
        warning in {"Web search failed.", "Search provider blocked automated access."}
        for warning in result.warnings
    )
    assert any("No sources retrieved" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_retrieval_mode_timeout_warning_is_user_friendly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "y")
    monkeypatch.setattr("orchestrator.router.OpenAIAdapter", lambda _cfg: FakeAdapter("openai"))
    monkeypatch.setattr("orchestrator.router.AnthropicAdapter", lambda _cfg: FakeAdapter("anthropic"))

    async def _timeout_search(*_args, **_kwargs):
        raise TimeoutError("timed out while searching")

    monkeypatch.setattr("orchestrator.router.search_web", _timeout_search)

    orchestrator = Orchestrator(_config(tmp_path))
    result = await orchestrator.ask("latest release notes", mode="retrieval")

    assert result.provider == "none"
    assert result.warnings is not None
    assert "Web search timed out." in result.warnings
    assert any("No sources retrieved" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_retrieval_mode_citations_only_renders_urls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "y")
    monkeypatch.setattr("orchestrator.router.OpenAIAdapter", lambda _cfg: PlaceholderCitationAdapter("openai"))
    monkeypatch.setattr("orchestrator.router.AnthropicAdapter", lambda _cfg: FakeAdapter("anthropic"))

    async def _fake_search(*_args, **_kwargs):
        from orchestrator.retrieval.search import SearchResult

        return [SearchResult(title="Doc", url="https://example.com/doc", snippet="Snippet")]

    async def _fake_fetch(url: str, **_kwargs):
        from orchestrator.retrieval.fetch import RetrievedDocument
        from orchestrator.security.taint import TaintedString

        return RetrievedDocument(
            url=url,
            title="Doc",
            retrieved_at="2026-02-10T00:00:00+00:00",
            text=TaintedString(
                value="UNTRUSTED_SOURCE_BEGIN\\nSource\\nUNTRUSTED_SOURCE_END",
                source="retrieved_text",
                source_id=url,
                taint_level="untrusted",
            ),
        )

    monkeypatch.setattr("orchestrator.router.search_web", _fake_search)
    monkeypatch.setattr("orchestrator.router.fetch_url_content", _fake_fetch)

    orchestrator = Orchestrator(_config(tmp_path))
    result = await orchestrator.ask("Find data and provide citations only.", mode="retrieval")

    assert result.citations is not None
    assert "https://example.com/doc" in result.answer


@pytest.mark.asyncio
async def test_retrieval_mode_shared_state_contains_diagnostics_schema(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "y")
    monkeypatch.setattr("orchestrator.router.OpenAIAdapter", lambda _cfg: FakeAdapter("openai"))
    monkeypatch.setattr("orchestrator.router.AnthropicAdapter", lambda _cfg: FakeAdapter("anthropic"))

    async def _failing_search(*_args, **_kwargs):
        raise RuntimeError("challenge page")

    monkeypatch.setattr("orchestrator.router.search_web", _failing_search)

    orchestrator = Orchestrator(_config(tmp_path))
    result = await orchestrator.ask("find docs for x", mode="retrieval")

    assert result.shared_state is not None
    summary = result.shared_state.get("summary")
    assert isinstance(summary, dict)
    assert summary.get("diagnostics_schema") == "retrieval.v1"

    stages = result.shared_state.get("stages")
    assert isinstance(stages, list)
    diagnostics_stage = next(
        (stage for stage in stages if isinstance(stage, dict) and stage.get("name") == "diagnostics"),
        None,
    )
    assert isinstance(diagnostics_stage, dict)
    assert diagnostics_stage.get("schema_version") == "retrieval.v1"
    data = diagnostics_stage.get("data")
    assert isinstance(data, dict)
    search = data.get("search")
    assert isinstance(search, dict)
    assert search.get("error_code") in {"challenge", "error"}
    timings = data.get("timings_ms")
    assert isinstance(timings, dict)
    for key in ("search_ms", "fetch_ms", "synthesis_ms", "total_ms"):
        assert key in timings
        assert isinstance(timings[key], int)


def test_extract_direct_fetch_urls_ignores_false_positive_domain_tokens(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "y")
    monkeypatch.setattr("orchestrator.router.OpenAIAdapter", lambda _cfg: FakeAdapter("openai"))
    monkeypatch.setattr("orchestrator.router.AnthropicAdapter", lambda _cfg: FakeAdapter("anthropic"))

    orchestrator = Orchestrator(_config(tmp_path))
    urls = orchestrator._extract_direct_fetch_urls("Can you explain Response.blob and Promise.resolve in JS?")
    assert urls == []


def test_retrieval_answer_style_auto_upgrades_for_full_detail_intent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "y")
    monkeypatch.setattr("orchestrator.router.OpenAIAdapter", lambda _cfg: FakeAdapter("openai"))
    monkeypatch.setattr("orchestrator.router.AnthropicAdapter", lambda _cfg: FakeAdapter("anthropic"))
    monkeypatch.setenv("MMO_RETRIEVAL_ANSWER_STYLE", "concise_ranked")

    orchestrator = Orchestrator(_config(tmp_path))
    assert (
        orchestrator._effective_retrieval_answer_style(
            "what is the full recipe from this page with step by step instructions"
        )
        == "full_details"
    )
