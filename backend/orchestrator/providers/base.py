from __future__ import annotations

from collections.abc import AsyncIterator
from abc import ABC, abstractmethod
from dataclasses import dataclass
import re

try:
    import tiktoken
except ModuleNotFoundError:  # pragma: no cover
    tiktoken = None

from orchestrator.config import ProviderConfig


@dataclass(slots=True)
class CompletionResult:
    text: str
    tokens_in: int
    tokens_out: int
    model: str
    latency_ms: int
    estimated_cost: float
    provider: str


class ProviderTimeoutError(RuntimeError):
    """Raised when a provider call exceeds the configured timeout."""


class ProviderAdapter(ABC):
    """Abstract provider adapter contract for model completion calls."""

    def __init__(self, provider_name: str, config: ProviderConfig, rate_limiter=None):
        self.provider_name = provider_name
        self.config = config
        self.rate_limiter = rate_limiter

    @abstractmethod
    async def complete(self, prompt: str, model: str, max_tokens: int, temperature: float) -> CompletionResult:
        """Return a plain text completion result."""

    @abstractmethod
    async def complete_structured(
        self,
        prompt: str,
        model: str,
        output_schema: dict,
        max_tokens: int,
        temperature: float,
    ) -> CompletionResult:
        """Return a completion intended to follow a structured schema."""

    @abstractmethod
    async def complete_stream(
        self,
        prompt: str,
        model: str,
        max_tokens: int,
        temperature: float,
    ) -> AsyncIterator[str]:
        """Yield streamed text chunks for a completion."""

    def count_tokens(self, text: str, model: str) -> int:
        """Estimate token count with tiktoken fallback to rough split."""
        if tiktoken is None:
            return max(1, len(text.split()))
        try:
            enc = tiktoken.encoding_for_model(model)
            return len(enc.encode(text))
        except Exception:
            return max(1, len(text.split()))

    def estimate_cost(self, tokens_in: int, tokens_out: int, model: str) -> float:
        """Estimate USD cost based on configured per-1M token pricing."""
        pricing_key = self._resolve_pricing_key(model)
        if pricing_key is None:
            return 0.0
        pricing = self.config.pricing_usd_per_1m_tokens.get(pricing_key)
        if not pricing:
            return 0.0
        return (tokens_in / 1_000_000 * pricing.input) + (tokens_out / 1_000_000 * pricing.output)

    def _resolve_pricing_key(self, model: str) -> str | None:
        """Resolve a configured pricing key for a model name returned by providers."""
        pricing = self.config.pricing_usd_per_1m_tokens
        if model in pricing:
            return model

        normalized = model.strip().lower()
        if not normalized:
            return None

        normalized_keys = {key.lower(): key for key in pricing.keys()}
        if normalized in normalized_keys:
            return normalized_keys[normalized]

        # Handle snapshot/date-like suffixes (for example: model-2026-02-15).
        compact = normalized
        while True:
            trimmed = re.sub(r"[-_:]?\d{4}(?:[-_]\d{2}){1,2}$", "", compact)
            if trimmed == compact:
                break
            compact = trimmed
            if compact in normalized_keys:
                return normalized_keys[compact]

        def _boundary_prefix(longer: str, shorter: str) -> bool:
            if not longer.startswith(shorter):
                return False
            if len(longer) == len(shorter):
                return True
            return longer[len(shorter)] in "-_:@/"

        candidates: list[tuple[int, str]] = []
        for key_norm, original in normalized_keys.items():
            if _boundary_prefix(normalized, key_norm) or _boundary_prefix(key_norm, normalized):
                candidates.append((len(key_norm), original))

        if candidates:
            candidates.sort(key=lambda item: item[0], reverse=True)
            return candidates[0][1]

        # Fallback to configured fast/deep price keys when using newer vendor model aliases.
        models_cfg = getattr(self.config, "models", None)
        fast_key = getattr(models_cfg, "fast", "")
        deep_key = getattr(models_cfg, "deep", "")
        for key in (fast_key, deep_key):
            if isinstance(key, str) and key in pricing:
                return key

        # Final fallback: deterministic first configured key.
        return next(iter(pricing.keys()), None)

    def timeout_for_model(self, model: str) -> float:
        """Return per-call timeout for the requested model tier."""
        if model == self.config.models.deep:
            return self.config.timeouts.deep_seconds
        return self.config.timeouts.standard_seconds

    async def acquire_rate_limit(self, *, prompt: str, model: str, max_tokens: int) -> None:
        """Acquire provider rate-limit permit for this request if limiter is configured."""
        if self.rate_limiter is None:
            return
        prompt_tokens = self.count_tokens(prompt, model)
        requested_tokens = max(1, prompt_tokens + max(0, max_tokens))
        await self.rate_limiter.acquire(self.provider_name, tokens=requested_tokens)
