from __future__ import annotations

import pytest

pytest.importorskip("openai")
pytest.importorskip("anthropic")

from orchestrator.config import ProviderConfig, ProviderModelConfig, ProviderPricing
from orchestrator.providers.anthropic_adapter import AnthropicAdapter
from orchestrator.providers.base import ProviderTimeoutError
from orchestrator.providers.openai_adapter import OpenAIAdapter


def _provider_config(
    env_name: str,
    model: str = "test-model",
    temperature_unsupported_models: list[str] | None = None,
) -> ProviderConfig:
    return ProviderConfig(
        enabled=True,
        api_key_env=env_name,
        models=ProviderModelConfig(fast=model, deep=model),
        pricing_usd_per_1m_tokens={model: ProviderPricing(input=1.0, output=2.0)},
        temperature_unsupported_models=temperature_unsupported_models or [],
    )


class _OpenAIUsage:
    prompt_tokens = 10
    completion_tokens = 20


class _OpenAIMessage:
    content = "ok"


class _OpenAIChoice:
    message = _OpenAIMessage()


class _OpenAIChatResponse:
    usage = _OpenAIUsage()
    choices = [_OpenAIChoice()]


class _OpenAIClient:
    def __init__(self, failures: int):
        self.failures = failures
        self.calls = 0
        self.chat = self
        self.completions = self

    async def create(self, **kwargs):  # noqa: ANN003
        self.calls += 1
        if self.calls <= self.failures:
            raise _RetryableOpenAI("limit")
        return _OpenAIChatResponse()


class _AnthropicBlock:
    type = "text"
    text = "ok"


class _AnthropicUsage:
    input_tokens = 9
    output_tokens = 7


class _AnthropicResponse:
    content = [_AnthropicBlock()]
    usage = _AnthropicUsage()


class _AnthropicClient:
    def __init__(self, failures: int):
        self.failures = failures
        self.calls = 0
        self.messages = self

    async def create(self, **kwargs):  # noqa: ANN003
        self.calls += 1
        if self.calls <= self.failures:
            raise _RetryableAnthropic("limit")
        return _AnthropicResponse()


class _RetryableOpenAI(Exception):
    pass


class _RetryableAnthropic(Exception):
    pass


class _NonRetryableOpenAI(Exception):
    status_code = 400


class _NonRetryableAnthropic(Exception):
    status_code = 400


class _TempUnsupportedOpenAI(Exception):
    status_code = 400


@pytest.mark.asyncio
async def test_openai_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setattr("orchestrator.providers.openai_adapter.RateLimitError", _RetryableOpenAI)

    async def _no_sleep(*_args, **_kwargs):  # noqa: ANN002, ANN003
        return None

    adapter = OpenAIAdapter(_provider_config("OPENAI_API_KEY"))
    adapter.client = _OpenAIClient(failures=2)
    monkeypatch.setattr("orchestrator.providers.openai_adapter.asyncio.sleep", _no_sleep)

    result = await adapter.complete("hello", "test-model", max_tokens=100, temperature=0.1)
    assert result.text == "ok"
    assert adapter.client.calls == 3


@pytest.mark.asyncio
async def test_anthropic_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setattr("orchestrator.providers.anthropic_adapter.RateLimitError", _RetryableAnthropic)

    async def _no_sleep(*_args, **_kwargs):  # noqa: ANN002, ANN003
        return None

    adapter = AnthropicAdapter(_provider_config("ANTHROPIC_API_KEY"))
    adapter.client = _AnthropicClient(failures=2)
    monkeypatch.setattr("orchestrator.providers.anthropic_adapter.asyncio.sleep", _no_sleep)

    result = await adapter.complete("hello", "test-model", max_tokens=100, temperature=0.1)
    assert result.text == "ok"
    assert adapter.client.calls == 3


@pytest.mark.asyncio
async def test_openai_timeout_raises_provider_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")

    async def _timeout(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise TimeoutError()

    monkeypatch.setattr("orchestrator.providers.openai_adapter.asyncio.wait_for", _timeout)
    monkeypatch.setattr("orchestrator.providers.openai_adapter.asyncio.sleep", lambda *_a, **_k: _no_op())  # type: ignore[arg-type]
    adapter = OpenAIAdapter(_provider_config("OPENAI_API_KEY"))
    adapter.client = _OpenAIClient(failures=0)

    with pytest.raises(ProviderTimeoutError):
        await adapter.complete("hello", "test-model", max_tokens=100, temperature=0.1)


@pytest.mark.asyncio
async def test_openai_non_retryable_error_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setattr("orchestrator.providers.openai_adapter.APIError", _NonRetryableOpenAI)
    monkeypatch.setattr("orchestrator.providers.openai_adapter.asyncio.sleep", lambda *_a, **_k: _no_op())  # type: ignore[arg-type]

    class _Client:
        def __init__(self):
            self.calls = 0
            self.chat = self
            self.completions = self

        async def create(self, **kwargs):  # noqa: ANN003
            self.calls += 1
            raise _NonRetryableOpenAI("bad request")

    adapter = OpenAIAdapter(_provider_config("OPENAI_API_KEY"))
    adapter.client = _Client()

    with pytest.raises(RuntimeError, match="non-retryable"):
        await adapter.complete("hello", "test-model", max_tokens=100, temperature=0.1)
    assert adapter.client.calls == 1


@pytest.mark.asyncio
async def test_openai_caches_temperature_unsupported_per_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setattr("orchestrator.providers.openai_adapter.APIError", _TempUnsupportedOpenAI)

    class _Client:
        def __init__(self):
            self.calls = 0
            self.calls_with_temperature = 0
            self.chat = self
            self.completions = self
            self._raised_once = False

        async def create(self, **kwargs):  # noqa: ANN003
            self.calls += 1
            if "temperature" in kwargs:
                self.calls_with_temperature += 1
                if not self._raised_once:
                    self._raised_once = True
                    raise _TempUnsupportedOpenAI("unsupported parameter: temperature")
            return _OpenAIChatResponse()

    adapter = OpenAIAdapter(_provider_config("OPENAI_API_KEY"))
    adapter.client = _Client()

    # First call: one rejected attempt with temperature, then success without.
    first = await adapter.complete("hello", "test-model", max_tokens=100, temperature=0.1)
    assert first.text == "ok"
    assert adapter.client.calls == 2
    assert adapter.client.calls_with_temperature == 1

    # Second call on same model: should skip temperature immediately.
    second = await adapter.complete("hello again", "test-model", max_tokens=100, temperature=0.1)
    assert second.text == "ok"
    assert adapter.client.calls == 3
    assert adapter.client.calls_with_temperature == 1


@pytest.mark.asyncio
async def test_openai_skips_temperature_when_preconfigured_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")

    class _Client:
        def __init__(self):
            self.calls = 0
            self.calls_with_temperature = 0
            self.chat = self
            self.completions = self

        async def create(self, **kwargs):  # noqa: ANN003
            self.calls += 1
            if "temperature" in kwargs:
                self.calls_with_temperature += 1
            return _OpenAIChatResponse()

    adapter = OpenAIAdapter(
        _provider_config(
            "OPENAI_API_KEY",
            model="test-model",
            temperature_unsupported_models=["test-model"],
        )
    )
    adapter.client = _Client()

    result = await adapter.complete("hello", "test-model", max_tokens=100, temperature=0.1)
    assert result.text == "ok"
    assert adapter.client.calls == 1
    assert adapter.client.calls_with_temperature == 0


@pytest.mark.asyncio
async def test_anthropic_timeout_raises_provider_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")

    async def _timeout(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise TimeoutError()

    monkeypatch.setattr("orchestrator.providers.anthropic_adapter.asyncio.wait_for", _timeout)
    monkeypatch.setattr("orchestrator.providers.anthropic_adapter.asyncio.sleep", lambda *_a, **_k: _no_op())  # type: ignore[arg-type]
    adapter = AnthropicAdapter(_provider_config("ANTHROPIC_API_KEY"))
    adapter.client = _AnthropicClient(failures=0)

    with pytest.raises(ProviderTimeoutError):
        await adapter.complete("hello", "test-model", max_tokens=100, temperature=0.1)


@pytest.mark.asyncio
async def test_anthropic_non_retryable_error_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setattr("orchestrator.providers.anthropic_adapter.APIStatusError", _NonRetryableAnthropic)
    monkeypatch.setattr("orchestrator.providers.anthropic_adapter.asyncio.sleep", lambda *_a, **_k: _no_op())  # type: ignore[arg-type]

    class _Client:
        def __init__(self):
            self.calls = 0
            self.messages = self

        async def create(self, **kwargs):  # noqa: ANN003
            self.calls += 1
            raise _NonRetryableAnthropic("invalid request")

    adapter = AnthropicAdapter(_provider_config("ANTHROPIC_API_KEY"))
    adapter.client = _Client()

    with pytest.raises(RuntimeError, match="non-retryable"):
        await adapter.complete("hello", "test-model", max_tokens=100, temperature=0.1)
    assert adapter.client.calls == 1


async def _no_op() -> None:
    return None
