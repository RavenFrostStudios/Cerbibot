from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("openai")

from orchestrator.config import ProviderConfig, ProviderModelConfig, ProviderPricing
from orchestrator.providers.base import ProviderTimeoutError
from orchestrator.providers.google_adapter import GoogleAdapter
from orchestrator.providers.xai_adapter import XAIAdapter


def _provider_config(env_name: str, model: str = "test-model") -> ProviderConfig:
    return ProviderConfig(
        enabled=True,
        api_key_env=env_name,
        models=ProviderModelConfig(fast=model, deep=model),
        pricing_usd_per_1m_tokens={model: ProviderPricing(input=1.0, output=2.0)},
    )


class _OpenAIUsage:
    prompt_tokens = 11
    completion_tokens = 13


class _OpenAIMessage:
    content = "ok-xai"


class _OpenAIChoice:
    message = _OpenAIMessage()


class _OpenAIChatResponse:
    usage = _OpenAIUsage()
    choices = [_OpenAIChoice()]


class _XAIClient:
    def __init__(self, failures: int = 0):
        self.failures = failures
        self.calls = 0
        self.chat = self
        self.completions = self

    async def create(self, **kwargs):  # noqa: ANN003
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("retry")
        return _OpenAIChatResponse()


class _XAINonRetryableClient(_XAIClient):
    async def create(self, **kwargs):  # noqa: ANN003
        self.calls += 1
        raise _NonRetryableXAI("invalid request")


class _NonRetryableXAI(Exception):
    status_code = 400


class _GoogleUsage:
    prompt_token_count = 7
    candidates_token_count = 9


class _GoogleUsageWithNone:
    prompt_token_count = None
    candidates_token_count = None


class _GoogleResponse:
    text = "ok-google"
    usage_metadata = _GoogleUsage()


class _GoogleResponseWithNoneUsage:
    text = "ok-google"
    usage_metadata = _GoogleUsageWithNone()


class _GoogleModels:
    def __init__(self, failures: int = 0):
        self.failures = failures
        self.calls = 0

    def generate_content(self, **kwargs):  # noqa: ANN003
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("retry")
        return _GoogleResponse()

    def generate_content_stream(self, **kwargs):  # noqa: ANN003
        return []


class _GoogleModelsNoneUsage(_GoogleModels):
    def generate_content(self, **kwargs):  # noqa: ANN003
        self.calls += 1
        return _GoogleResponseWithNoneUsage()


class _GoogleClient:
    def __init__(self, failures: int = 0):
        self.models = _GoogleModels(failures=failures)


class _GoogleClientNoneUsage:
    def __init__(self):
        self.models = _GoogleModelsNoneUsage(failures=0)


class _GoogleNonRetryableModels(_GoogleModels):
    def generate_content(self, **kwargs):  # noqa: ANN003
        self.calls += 1
        raise RuntimeError("invalid argument")


class _GoogleNonRetryableClient:
    def __init__(self):
        self.models = _GoogleNonRetryableModels(failures=0)


@pytest.mark.asyncio
async def test_xai_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XAI_API_KEY", "x")
    adapter = XAIAdapter(_provider_config("XAI_API_KEY"))
    adapter.client = _XAIClient(failures=0)
    result = await adapter.complete("hello", "test-model", max_tokens=100, temperature=0.1)
    assert result.text == "ok-xai"
    assert result.tokens_in == 11
    assert result.tokens_out == 13


@pytest.mark.asyncio
async def test_xai_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XAI_API_KEY", "x")
    adapter = XAIAdapter(_provider_config("XAI_API_KEY"))
    adapter.client = _XAIClient(failures=0)

    async def _timeout(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise TimeoutError()

    monkeypatch.setattr("orchestrator.providers.xai_adapter.asyncio.wait_for", _timeout)
    monkeypatch.setattr("orchestrator.providers.xai_adapter.asyncio.sleep", _no_op_sleep)
    with pytest.raises(ProviderTimeoutError):
        await adapter.complete("hello", "test-model", max_tokens=100, temperature=0.1)


@pytest.mark.asyncio
async def test_google_complete_without_sdk_init() -> None:
    adapter = GoogleAdapter.__new__(GoogleAdapter)
    from orchestrator.providers.base import ProviderAdapter

    ProviderAdapter.__init__(adapter, "google", _provider_config("GOOGLE_API_KEY"))
    adapter.client = _GoogleClient(failures=0)
    result = await adapter.complete("hello", "test-model", max_tokens=100, temperature=0.1)
    assert result.text == "ok-google"
    assert result.tokens_in == 7
    assert result.tokens_out == 9


@pytest.mark.asyncio
async def test_google_complete_handles_none_usage_metadata() -> None:
    adapter = GoogleAdapter.__new__(GoogleAdapter)
    from orchestrator.providers.base import ProviderAdapter

    ProviderAdapter.__init__(adapter, "google", _provider_config("GOOGLE_API_KEY"))
    adapter.client = _GoogleClientNoneUsage()
    result = await adapter.complete("hello", "test-model", max_tokens=100, temperature=0.1)
    assert result.text == "ok-google"
    assert result.tokens_in > 0
    assert result.tokens_out > 0


@pytest.mark.asyncio
async def test_google_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = GoogleAdapter.__new__(GoogleAdapter)
    from orchestrator.providers.base import ProviderAdapter

    ProviderAdapter.__init__(adapter, "google", _provider_config("GOOGLE_API_KEY"))
    adapter.client = _GoogleClient(failures=0)

    async def _timeout(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise TimeoutError()

    monkeypatch.setattr("orchestrator.providers.google_adapter.asyncio.wait_for", _timeout)
    monkeypatch.setattr("orchestrator.providers.google_adapter.asyncio.sleep", _no_op_sleep)
    with pytest.raises(ProviderTimeoutError):
        await adapter.complete("hello", "test-model", max_tokens=100, temperature=0.1)


@pytest.mark.asyncio
async def test_xai_non_retryable_error_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XAI_API_KEY", "x")
    monkeypatch.setattr("orchestrator.providers.xai_adapter.APIError", _NonRetryableXAI)
    monkeypatch.setattr("orchestrator.providers.xai_adapter.asyncio.sleep", _no_op_sleep)
    adapter = XAIAdapter(_provider_config("XAI_API_KEY"))
    adapter.client = _XAINonRetryableClient(failures=0)
    with pytest.raises(RuntimeError, match="non-retryable"):
        await adapter.complete("hello", "test-model", max_tokens=100, temperature=0.1)
    assert adapter.client.calls == 1


@pytest.mark.asyncio
async def test_google_non_retryable_error_fails_fast() -> None:
    adapter = GoogleAdapter.__new__(GoogleAdapter)
    from orchestrator.providers.base import ProviderAdapter

    ProviderAdapter.__init__(adapter, "google", _provider_config("GOOGLE_API_KEY"))
    adapter.client = _GoogleNonRetryableClient()
    with pytest.raises(RuntimeError, match="non-retryable"):
        await adapter.complete("hello", "test-model", max_tokens=100, temperature=0.1)
    assert adapter.client.models.calls == 1


async def _no_op_sleep(*_args, **_kwargs):  # noqa: ANN002, ANN003
    return None
