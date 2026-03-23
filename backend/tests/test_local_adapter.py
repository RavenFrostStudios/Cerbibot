from __future__ import annotations

import pytest

pytest.importorskip("openai")

from orchestrator.config import ProviderConfig, ProviderModelConfig, ProviderPricing
from orchestrator.providers.local_adapter import LocalAdapter


def _provider_config(env_name: str, model: str = "local-model") -> ProviderConfig:
    return ProviderConfig(
        enabled=True,
        api_key_env=env_name,
        models=ProviderModelConfig(fast=model, deep=model),
        pricing_usd_per_1m_tokens={model: ProviderPricing(input=0.0, output=0.0)},
    )


class _Usage:
    prompt_tokens = 10
    completion_tokens = 20


class _Message:
    content = "local-ok"


class _Choice:
    message = _Message()


class _Response:
    usage = _Usage()
    choices = [_Choice()]


class _BlockMessage:
    content = [{"type": "text", "text": "local-from-block"}]


class _BlockChoice:
    message = _BlockMessage()


class _BlockResponse:
    usage = _Usage()
    choices = [_BlockChoice()]


class _ReasoningMessage:
    content = None
    reasoning = [{"type": "reasoning", "text": "local-from-reasoning"}]


class _ReasoningChoice:
    message = _ReasoningMessage()


class _ReasoningResponse:
    usage = _Usage()
    choices = [_ReasoningChoice()]


class _ModelItem:
    def __init__(self, model_id: str):
        self.id = model_id


class _ModelListResponse:
    def __init__(self, ids: list[str]):
        self.data = [_ModelItem(i) for i in ids]


class _FakeClient:
    def __init__(self):
        self.chat = self
        self.completions = self
        self.models = self
        self.called_model = None

    async def create(self, **kwargs):  # noqa: ANN003
        self.called_model = kwargs.get("model")
        return _Response()

    async def list(self):
        return _ModelListResponse(["detected-model"])


class _BlockClient(_FakeClient):
    async def create(self, **kwargs):  # noqa: ANN003
        self.called_model = kwargs.get("model")
        return _BlockResponse()


class _ReasoningClient(_FakeClient):
    async def create(self, **kwargs):  # noqa: ANN003
        self.called_model = kwargs.get("model")
        return _ReasoningResponse()


@pytest.mark.asyncio
async def test_local_adapter_detects_and_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCAL_API_KEY", "x")
    adapter = LocalAdapter(_provider_config("LOCAL_API_KEY", model="configured-model"))
    adapter.client = _FakeClient()

    result = await adapter.complete("hello", "configured-model", max_tokens=50, temperature=0.2)
    assert result.text == "local-ok"
    assert adapter.available_models == ["detected-model"]
    assert adapter.client.called_model == "detected-model"


@pytest.mark.asyncio
async def test_local_adapter_extracts_text_from_block_content(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCAL_API_KEY", "x")
    adapter = LocalAdapter(_provider_config("LOCAL_API_KEY", model="configured-model"))
    adapter.client = _BlockClient()

    result = await adapter.complete("hello", "configured-model", max_tokens=50, temperature=0.2)
    assert result.text == "local-from-block"


@pytest.mark.asyncio
async def test_local_adapter_extracts_text_from_reasoning_field(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCAL_API_KEY", "x")
    adapter = LocalAdapter(_provider_config("LOCAL_API_KEY", model="configured-model"))
    adapter.client = _ReasoningClient()

    result = await adapter.complete("hello", "configured-model", max_tokens=50, temperature=0.2)
    assert result.text == "local-from-reasoning"
