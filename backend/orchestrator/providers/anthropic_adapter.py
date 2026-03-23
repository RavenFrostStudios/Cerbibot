from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import logging
import os
import time

from anthropic import APIConnectionError, APIStatusError, APITimeoutError, AsyncAnthropic, RateLimitError

from orchestrator.config import ProviderConfig
from orchestrator.providers.base import CompletionResult, ProviderAdapter, ProviderTimeoutError
from orchestrator.providers.retry_utils import extract_status_code, is_retryable_error, retry_backoff_seconds


logger = logging.getLogger(__name__)


class AnthropicAdapter(ProviderAdapter):
    """Anthropic provider adapter with retry/backoff and cost metadata."""

    def __init__(self, config: ProviderConfig, rate_limiter=None):
        super().__init__(provider_name="anthropic", config=config, rate_limiter=rate_limiter)
        key = os.getenv(config.api_key_env, "")
        client_timeout = max(config.timeouts.standard_seconds, config.timeouts.deep_seconds)
        self.client = AsyncAnthropic(api_key=key, timeout=client_timeout)

    async def complete(self, prompt: str, model: str, max_tokens: int, temperature: float) -> CompletionResult:
        return await self._complete(prompt, model, max_tokens, temperature)

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
        return await self._complete(prompt, model, max_tokens, temperature, system_prompt=instruction)

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
        try:
            async with self.client.messages.stream(
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                system="You are a helpful assistant.",
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                async for text in stream.text_stream:
                    if time.monotonic() - start > timeout_seconds:
                        raise ProviderTimeoutError(f"Anthropic stream timed out after {timeout_seconds}s")
                    if text:
                        yield text
        except APIConnectionError as err:
            raise RuntimeError(f"Anthropic streaming connection failed: {err}") from err

    async def _complete(
        self,
        prompt: str,
        model: str,
        max_tokens: int,
        temperature: float,
        system_prompt: str = "You are a helpful assistant.",
    ) -> CompletionResult:
        start = time.monotonic()
        attempts = 3
        last_error = None
        timeout_seconds = self.timeout_for_model(model)
        await self.acquire_rate_limit(prompt=prompt, model=model, max_tokens=max_tokens)

        for i in range(attempts):
            try:
                request_coro = self.client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    system=system_prompt,
                    messages=[{"role": "user", "content": prompt}],
                )
                response = await asyncio.wait_for(
                    request_coro,
                    timeout=timeout_seconds,
                )
                parts = [block.text for block in response.content if getattr(block, "type", "") == "text"]
                text = "\n".join(parts)
                tokens_in = int(getattr(response.usage, "input_tokens", self.count_tokens(prompt, model)))
                tokens_out = int(getattr(response.usage, "output_tokens", self.count_tokens(text, model)))
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
            except (RateLimitError, APIConnectionError, APITimeoutError, APIStatusError) as err:
                if not is_retryable_error(
                    err,
                    retryable_types=(RateLimitError, APIConnectionError, APITimeoutError),
                ):
                    status_code = extract_status_code(err)
                    raise RuntimeError(
                        f"Anthropic completion failed with non-retryable error"
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
                f"Anthropic call timed out after {attempts} attempts (timeout={timeout_seconds}s)"
            ) from last_error
        raise RuntimeError(f"Anthropic completion failed after retries: {last_error}")
