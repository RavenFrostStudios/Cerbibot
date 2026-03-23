from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import logging
import os
import random
import time

from openai import APIConnectionError, APIError, APITimeoutError, AsyncOpenAI, RateLimitError

from orchestrator.config import ProviderConfig
from orchestrator.providers.base import CompletionResult, ProviderAdapter, ProviderTimeoutError


logger = logging.getLogger(__name__)


class LocalAdapter(ProviderAdapter):
    """OpenAI-compatible adapter for local model servers (vLLM/llama.cpp/Ollama gateways)."""

    def __init__(self, config: ProviderConfig, rate_limiter=None):
        super().__init__(provider_name="local", config=config, rate_limiter=rate_limiter)
        key = os.getenv(config.api_key_env, "local")
        base_url = os.getenv("LOCAL_API_BASE", "http://127.0.0.1:11434/v1")
        min_timeout = float(os.getenv("MMO_LOCAL_MIN_TIMEOUT_SECONDS", "120"))
        client_timeout = max(config.timeouts.standard_seconds, config.timeouts.deep_seconds, min_timeout)
        self.client = AsyncOpenAI(api_key=key, base_url=base_url, timeout=client_timeout)
        self.available_models: list[str] = []
        self._detected = False

    async def detect_models(self) -> list[str]:
        if self._detected:
            return self.available_models
        self._detected = True
        try:
            response = await asyncio.wait_for(self.client.models.list(), timeout=self.config.timeouts.standard_seconds)
            names = []
            for item in getattr(response, "data", []) or []:
                model_id = str(getattr(item, "id", "")).strip()
                if model_id:
                    names.append(model_id)
            self.available_models = sorted(set(names))
            if self.available_models:
                logger.info("local_models_detected", extra={"count": len(self.available_models), "models": self.available_models})
        except Exception as exc:
            logger.warning("local_model_detection_failed", extra={"error": str(exc)})
            self.available_models = []
        return self.available_models

    async def complete(self, prompt: str, model: str, max_tokens: int, temperature: float) -> CompletionResult:
        await self.detect_models()
        resolved_model = self._resolve_model(model)
        return await self._complete(
            prompt=prompt,
            model=resolved_model,
            max_tokens=max_tokens,
            temperature=temperature,
            system_prompt="You are a helpful local assistant.",
        )

    async def complete_structured(
        self,
        prompt: str,
        model: str,
        output_schema: dict,
        max_tokens: int,
        temperature: float,
    ) -> CompletionResult:
        await self.detect_models()
        resolved_model = self._resolve_model(model)
        instruction = (
            "Return JSON only, matching these keys exactly: "
            f"{', '.join(output_schema.keys())}. No markdown, no prose."
        )
        return await self._complete(
            prompt=prompt,
            model=resolved_model,
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
        await self.detect_models()
        resolved_model = self._resolve_model(model)
        await self.acquire_rate_limit(prompt=prompt, model=resolved_model, max_tokens=max_tokens)
        min_timeout = float(os.getenv("MMO_LOCAL_MIN_TIMEOUT_SECONDS", "120"))
        timeout_seconds = max(self.timeout_for_model(resolved_model), min_timeout)
        start = time.monotonic()
        try:
            stream = await self.client.chat.completions.create(
                model=resolved_model,
                messages=[
                    {"role": "system", "content": "You are a helpful local assistant."},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=max_tokens,
                temperature=temperature,
                stream=True,
            )
            async for chunk in stream:
                if time.monotonic() - start > timeout_seconds:
                    raise ProviderTimeoutError(f"Local model stream timed out after {timeout_seconds}s")
                delta = chunk.choices[0].delta if chunk.choices else None
                text = self._extract_text(delta)
                if text:
                    yield text
        except APIConnectionError as err:
            raise RuntimeError(f"Local streaming connection failed: {err}") from err

    async def _complete(
        self,
        prompt: str,
        model: str,
        max_tokens: int,
        temperature: float,
        system_prompt: str,
    ) -> CompletionResult:
        start = time.monotonic()
        attempts = 2
        last_error = None
        min_timeout = float(os.getenv("MMO_LOCAL_MIN_TIMEOUT_SECONDS", "120"))
        timeout_seconds = max(self.timeout_for_model(model), min_timeout)
        await self.acquire_rate_limit(prompt=prompt, model=model, max_tokens=max_tokens)

        for i in range(attempts):
            try:
                request_coro = self.client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt},
                    ],
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                response = await asyncio.wait_for(
                    request_coro,
                    timeout=timeout_seconds,
                )
                choice = response.choices[0] if getattr(response, "choices", None) else None
                text = self._extract_text(getattr(choice, "message", None))
                if not text:
                    text = self._extract_text(getattr(choice, "delta", None))
                if not text:
                    text = self._extract_text(choice)
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
                if i == attempts - 1:
                    break
                await asyncio.sleep((2**i) + random.uniform(0.0, 0.2))
            except (RateLimitError, APIConnectionError, APITimeoutError, APIError) as err:
                last_error = err
                if i == attempts - 1:
                    break
                await asyncio.sleep((2**i) + random.uniform(0.0, 0.2))

        if isinstance(last_error, asyncio.TimeoutError):
            raise ProviderTimeoutError(
                f"Local call timed out after {attempts} attempts (timeout={timeout_seconds}s)"
            ) from last_error
        raise RuntimeError(f"Local completion failed after retries: {last_error}")

    def _resolve_model(self, requested: str) -> str:
        if not self.available_models:
            return requested
        if requested in self.available_models:
            return requested
        fallback = self.available_models[0]
        logger.warning(
            "local_model_fallback",
            extra={"requested": requested, "fallback": fallback},
        )
        return fallback

    def _extract_text(self, value, depth: int = 0) -> str:  # noqa: ANN001
        if depth > 6:
            return ""
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            parts = [self._extract_text(item, depth + 1) for item in value]
            return "\n".join(part for part in parts if part)
        if isinstance(value, dict):
            for key in (
                "content",
                "text",
                "value",
                "reasoning_content",
                "reasoning",
                "output_text",
                "response",
                "completion",
                "answer",
                "message",
                "parts",
            ):
                if key in value:
                    text = self._extract_text(value.get(key), depth + 1)
                    if text:
                        return text
            for nested in value.values():
                if isinstance(nested, (dict, list)):
                    text = self._extract_text(nested, depth + 1)
                    if text:
                        return text
            return ""
        if hasattr(value, "model_dump") and callable(getattr(value, "model_dump")):
            try:
                dumped = value.model_dump()
                text = self._extract_text(dumped, depth + 1)
                if text:
                    return text
            except Exception:
                pass
        for attr in ("content", "text", "value", "reasoning_content", "reasoning", "output_text", "response"):
            if hasattr(value, attr):
                text = self._extract_text(getattr(value, attr), depth + 1)
                if text:
                    return text
        if hasattr(value, "__dict__"):
            try:
                text = self._extract_text(vars(value), depth + 1)
                if text:
                    return text
            except Exception:
                pass
        return ""
