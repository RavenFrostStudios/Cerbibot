from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import logging
import os
import time
from typing import Any

from orchestrator.config import ProviderConfig
from orchestrator.providers.base import CompletionResult, ProviderAdapter, ProviderTimeoutError
from orchestrator.providers.retry_utils import extract_status_code, is_retryable_error, retry_backoff_seconds


logger = logging.getLogger(__name__)


class GoogleAdapter(ProviderAdapter):
    """Google Gemini provider adapter using google-genai SDK."""

    def __init__(self, config: ProviderConfig, rate_limiter=None):
        super().__init__(provider_name="google", config=config, rate_limiter=rate_limiter)
        key = os.getenv(config.api_key_env, "")
        try:
            from google import genai  # type: ignore
        except ModuleNotFoundError as exc:  # pragma: no cover
            raise RuntimeError("google-genai dependency is not installed") from exc
        self.client = genai.Client(api_key=key)

    async def complete(self, prompt: str, model: str, max_tokens: int, temperature: float) -> CompletionResult:
        return await self._complete(
            prompt=prompt,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system_instruction="You are a helpful assistant.",
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
            system_instruction=instruction,
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
        system_instruction = "You are a helpful assistant."
        config = {
            "temperature": temperature,
            "max_output_tokens": max_tokens,
            "system_instruction": system_instruction,
            # Keep generation text-only in orchestration workflows.
            # This avoids SDK-side AFC/tool loops from interfering with role prompts.
            "tools": [],
            "automatic_function_calling": {"disable": True},
        }
        try:
            request_coro = asyncio.to_thread(
                self.client.models.generate_content_stream,
                model=model,
                contents=prompt,
                config=config,
            )
            stream = await asyncio.wait_for(
                request_coro,
                timeout=timeout_seconds,
            )
            for chunk in stream:
                if time.monotonic() - start > timeout_seconds:
                    raise ProviderTimeoutError(f"Google stream timed out after {timeout_seconds}s")
                text = getattr(chunk, "text", None)
                if text:
                    yield str(text)
        except asyncio.TimeoutError as err:
            request_coro.close()
            raise ProviderTimeoutError(f"Google stream timed out after {timeout_seconds}s") from err
        except Exception as err:
            raise RuntimeError(f"Google streaming connection failed: {err}") from err

    async def _complete(
        self,
        prompt: str,
        model: str,
        max_tokens: int,
        temperature: float,
        system_instruction: str,
    ) -> CompletionResult:
        start = time.monotonic()
        attempts = 3
        last_error: Exception | None = None
        timeout_seconds = self.timeout_for_model(model)
        await self.acquire_rate_limit(prompt=prompt, model=model, max_tokens=max_tokens)

        config = {
            "temperature": temperature,
            "max_output_tokens": max_tokens,
            "system_instruction": system_instruction,
            # Keep generation text-only in orchestration workflows.
            "tools": [],
            "automatic_function_calling": {"disable": True},
        }

        for i in range(attempts):
            try:
                request_coro = asyncio.to_thread(
                    self.client.models.generate_content,
                    model=model,
                    contents=prompt,
                    config=config,
                )
                response = await asyncio.wait_for(
                    request_coro,
                    timeout=timeout_seconds,
                )
                text = _extract_google_text(response)
                usage = getattr(response, "usage_metadata", None)
                tokens_in = _safe_int(
                    getattr(usage, "prompt_token_count", None),
                    fallback=self.count_tokens(prompt, model),
                )
                tokens_out = _safe_int(
                    getattr(usage, "candidates_token_count", None),
                    fallback=self.count_tokens(text, model),
                )
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
            except Exception as err:
                if not is_retryable_error(err):
                    status_code = extract_status_code(err)
                    raise RuntimeError(
                        f"Google completion failed with non-retryable error"
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
                f"Google call timed out after {attempts} attempts (timeout={timeout_seconds}s)"
            ) from last_error
        raise RuntimeError(f"Google completion failed after retries: {last_error}")


def _extract_google_text(response: Any) -> str:
    text = getattr(response, "text", None)
    if isinstance(text, str) and text:
        return text
    candidates = getattr(response, "candidates", None) or []
    parts: list[str] = []
    for cand in candidates:
        content = getattr(cand, "content", None)
        cand_parts = getattr(content, "parts", None) or []
        for part in cand_parts:
            part_text = getattr(part, "text", None)
            if part_text:
                parts.append(str(part_text))
    return "\n".join(parts)


def _safe_int(value: Any, *, fallback: int) -> int:
    if value is None:
        return int(fallback)
    try:
        return int(value)
    except Exception:
        return int(fallback)
