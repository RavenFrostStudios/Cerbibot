from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import logging
import os
import time

from openai import APIConnectionError, APIError, APITimeoutError, AsyncOpenAI, RateLimitError

from orchestrator.config import ProviderConfig
from orchestrator.providers.base import CompletionResult, ProviderAdapter, ProviderTimeoutError
from orchestrator.providers.retry_utils import extract_status_code, is_retryable_error, retry_backoff_seconds


logger = logging.getLogger(__name__)


class OpenAIAdapter(ProviderAdapter):
    """OpenAI provider adapter with retry/backoff and cost metadata."""

    def __init__(self, config: ProviderConfig, rate_limiter=None):
        super().__init__(provider_name="openai", config=config, rate_limiter=rate_limiter)
        key = os.getenv(config.api_key_env, "")
        client_timeout = max(config.timeouts.standard_seconds, config.timeouts.deep_seconds)
        self.client = AsyncOpenAI(api_key=key, timeout=client_timeout)
        # Capability cache: models that reject temperature should not keep retrying that parameter.
        self._temperature_unsupported_models: set[str] = set(config.temperature_unsupported_models or [])

    async def complete(self, prompt: str, model: str, max_tokens: int, temperature: float) -> CompletionResult:
        return await self._complete(
            prompt=prompt,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system_prompt="You are a helpful assistant.",
        )

    async def complete_structured(
        self,
        prompt: str,
        model: str,
        output_schema: dict,
        max_tokens: int,
        temperature: float,
    ) -> CompletionResult:
        instruction = (
            "Return JSON only, matching these keys exactly: "
            f"{', '.join(output_schema.keys())}. No markdown, no prose."
        )
        return await self._complete(
            prompt=prompt,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system_prompt=instruction,
        )

    async def complete_stream(
        self,
        prompt: str,
        model: str,
        max_tokens: int,
        temperature: float,
    ) -> AsyncIterator[str]:
        await self.acquire_rate_limit(prompt=prompt, model=model, max_tokens=max_tokens)
        timeout_seconds = self.timeout_for_model(model)
        start = time.monotonic()
        include_temperature = model not in self._temperature_unsupported_models
        try:
            kwargs = {
                "model": model,
                "messages": [
                    {"role": "system", "content": "You are a helpful assistant."},
                    {"role": "user", "content": prompt},
                ],
                "max_completion_tokens": max_tokens,
                "stream": True,
            }
            if include_temperature:
                kwargs["temperature"] = temperature
            try:
                stream = await self.client.chat.completions.create(**kwargs)
            except APIError as err:
                if include_temperature and self._is_temperature_unsupported(err):
                    self._temperature_unsupported_models.add(model)
                    include_temperature = False
                    kwargs.pop("temperature", None)
                    stream = await self.client.chat.completions.create(**kwargs)
                else:
                    raise
            async for chunk in stream:
                if time.monotonic() - start > timeout_seconds:
                    raise ProviderTimeoutError(f"OpenAI stream timed out after {timeout_seconds}s")
                delta = chunk.choices[0].delta if chunk.choices else None
                text = getattr(delta, "content", None)
                if text:
                    yield text
        except APIConnectionError as err:
            raise RuntimeError(f"OpenAI streaming connection failed: {err}") from err

    async def _complete(
        self,
        prompt: str,
        model: str,
        max_tokens: int,
        temperature: float,
        system_prompt: str,
    ) -> CompletionResult:
        start = time.monotonic()
        attempts = 3
        last_error = None
        timeout_seconds = self.timeout_for_model(model)
        await self.acquire_rate_limit(prompt=prompt, model=model, max_tokens=max_tokens)
        include_temperature = model not in self._temperature_unsupported_models

        for i in range(attempts):
            try:
                kwargs = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt},
                    ],
                    "max_completion_tokens": max_tokens,
                }
                if include_temperature:
                    kwargs["temperature"] = temperature
                request_coro = self.client.chat.completions.create(**kwargs)
                response = await asyncio.wait_for(
                    request_coro,
                    timeout=timeout_seconds,
                )
                text = self._extract_text_from_response(response)
                usage = getattr(response, "usage", None)
                tokens_in = int(getattr(usage, "prompt_tokens", self.count_tokens(prompt, model)))
                tokens_out = int(getattr(usage, "completion_tokens", self.count_tokens(text, model)))
                return CompletionResult(
                    text=text,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    model=model,
                    latency_ms=int((time.monotonic() - start) * 1000),
                    estimated_cost=self.estimate_cost(tokens_in, tokens_out, model),
                    provider=self.provider_name,
                )
            except asyncio.TimeoutError as err:
                last_error = err
                request_coro.close()
                logger.warning(
                    "provider_timeout",
                    extra={
                        "provider": self.provider_name,
                        "model": model,
                        "attempt": i + 1,
                        "timeout_seconds": timeout_seconds,
                    },
                )
                if i == attempts - 1:
                    break
                await asyncio.sleep(retry_backoff_seconds(i))
            except (RateLimitError, APIConnectionError, APITimeoutError, APIError) as err:
                if include_temperature and self._is_temperature_unsupported(err):
                    self._temperature_unsupported_models.add(model)
                    include_temperature = False
                    logger.info(
                        "provider_temperature_unsupported_fallback",
                        extra={"provider": self.provider_name, "model": model},
                    )
                    continue
                if not is_retryable_error(
                    err,
                    retryable_types=(RateLimitError, APIConnectionError, APITimeoutError),
                ):
                    status_code = extract_status_code(err)
                    raise RuntimeError(
                        f"OpenAI completion failed with non-retryable error"
                        f"{f' (status={status_code})' if status_code is not None else ''}: {err}"
                    ) from err
                last_error = err
                logger.warning(
                    "provider_retryable_error",
                    extra={
                        "provider": self.provider_name,
                        "model": model,
                        "attempt": i + 1,
                        "error_type": type(err).__name__,
                        "status_code": extract_status_code(err),
                    },
                )
                if i == attempts - 1:
                    break
                await asyncio.sleep(retry_backoff_seconds(i))

        if isinstance(last_error, asyncio.TimeoutError):
            raise ProviderTimeoutError(
                f"OpenAI call timed out after {attempts} attempts (timeout={timeout_seconds}s)"
            ) from last_error
        raise RuntimeError(f"OpenAI completion failed after retries: {last_error}")

    @staticmethod
    def _extract_text_from_response(response) -> str:
        try:
            message = response.choices[0].message
        except Exception:
            return ""

        content = getattr(message, "content", None)
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                    continue
                if isinstance(item, dict):
                    text = item.get("text") or item.get("value")
                    if isinstance(text, str):
                        parts.append(text)
                    continue
                text = getattr(item, "text", None)
                if isinstance(text, str):
                    parts.append(text)
            return "\n".join(part for part in parts if part).strip()
        return ""

    @staticmethod
    def _is_temperature_unsupported(err: APIError) -> bool:
        msg = str(err).lower()
        return "temperature" in msg and ("does not support" in msg or "unsupported value" in msg or "unsupported parameter" in msg)
