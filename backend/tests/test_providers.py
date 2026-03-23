from dataclasses import dataclass

from orchestrator.providers.base import CompletionResult, ProviderAdapter


@dataclass
class FakePricing:
    input: float
    output: float


@dataclass
class FakeProviderCfg:
    pricing_usd_per_1m_tokens: dict


class FakeProvider(ProviderAdapter):
    def __init__(self) -> None:
        cfg = FakeProviderCfg(pricing_usd_per_1m_tokens={"test-model": FakePricing(input=1.0, output=2.0)})
        super().__init__("fake", cfg)

    async def complete(self, prompt: str, model: str, max_tokens: int, temperature: float) -> CompletionResult:
        return CompletionResult("x", 1, 1, model, 1, 0.0, "fake")

    async def complete_structured(self, prompt: str, model: str, output_schema: dict, max_tokens: int, temperature: float) -> CompletionResult:
        return CompletionResult("{}", 1, 1, model, 1, 0.0, "fake")

    async def complete_stream(self, prompt: str, model: str, max_tokens: int, temperature: float):
        yield "x"


def test_provider_estimate_cost() -> None:
    provider = FakeProvider()
    cost = provider.estimate_cost(tokens_in=1000, tokens_out=2000, model="test-model")
    assert round(cost, 6) == round(0.001 * 1.0 + 0.002 * 2.0, 6)


def test_provider_count_tokens_fallback() -> None:
    provider = FakeProvider()
    count = provider.count_tokens("one two three", "unknown-model")
    assert count >= 1


def test_provider_estimate_cost_with_snapshot_suffix() -> None:
    provider = FakeProvider()
    cost = provider.estimate_cost(tokens_in=1_000_000, tokens_out=1_000_000, model="test-model-2026-02-16")
    assert round(cost, 6) == round(1.0 + 2.0, 6)


def test_provider_estimate_cost_with_variant_prefix() -> None:
    provider = FakeProvider()
    cost = provider.estimate_cost(tokens_in=1_000_000, tokens_out=0, model="test-model:reasoning")
    assert round(cost, 6) == round(1.0, 6)


def test_provider_estimate_cost_falls_back_to_configured_model_key() -> None:
    provider = FakeProvider()
    cost = provider.estimate_cost(tokens_in=1_000_000, tokens_out=1_000_000, model="unknown-new-model")
    assert round(cost, 6) == round(1.0 + 2.0, 6)
