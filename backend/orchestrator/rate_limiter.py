from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from time import monotonic


@dataclass(slots=True)
class ProviderRateLimits:
    rpm: int
    tpm: int
    max_wait_seconds: float


class RateLimitExceededError(RuntimeError):
    """Raised when provider rate limit wait exceeds max_wait_seconds."""


class RateLimiter:
    """Sliding-window per-provider RPM/TPM limiter with async-safe acquire."""

    def __init__(self, limits: dict[str, ProviderRateLimits]):
        self.limits = limits
        self._req_events: dict[str, deque[float]] = {name: deque() for name in limits}
        self._tok_events: dict[str, deque[tuple[float, int]]] = {name: deque() for name in limits}
        self._lock = asyncio.Lock()

    async def acquire(self, provider: str, *, tokens: int = 0) -> None:
        if provider not in self.limits:
            return
        limits = self.limits[provider]
        started = monotonic()
        while True:
            async with self._lock:
                now = monotonic()
                self._prune(provider, now)
                rpm_used = len(self._req_events[provider])
                tpm_used = sum(t for _, t in self._tok_events[provider])
                rpm_ok = rpm_used < limits.rpm
                tpm_ok = (tpm_used + max(0, tokens)) <= limits.tpm
                if rpm_ok and tpm_ok:
                    self._req_events[provider].append(now)
                    self._tok_events[provider].append((now, max(0, tokens)))
                    return
                wait_for = self._next_wait_seconds(provider, now)

            elapsed = monotonic() - started
            if elapsed + wait_for > limits.max_wait_seconds:
                raise RateLimitExceededError(
                    f"Rate limit exceeded for provider={provider} (rpm={limits.rpm}, tpm={limits.tpm})"
                )
            await asyncio.sleep(max(0.01, wait_for))

    def headroom(self, provider: str) -> dict[str, float]:
        limits = self.limits.get(provider)
        if limits is None:
            return {"rpm_headroom": 1.0, "tpm_headroom": 1.0}
        now = monotonic()
        self._prune(provider, now)
        rpm_used = len(self._req_events[provider])
        tpm_used = sum(t for _, t in self._tok_events[provider])
        rpm_headroom = max(0.0, min(1.0, (limits.rpm - rpm_used) / max(1, limits.rpm)))
        tpm_headroom = max(0.0, min(1.0, (limits.tpm - tpm_used) / max(1, limits.tpm)))
        return {"rpm_headroom": rpm_headroom, "tpm_headroom": tpm_headroom}

    def snapshot(self) -> dict[str, dict[str, float | int]]:
        out: dict[str, dict[str, float | int]] = {}
        now = monotonic()
        for provider, limits in self.limits.items():
            self._prune(provider, now)
            rpm_used = len(self._req_events[provider])
            tpm_used = sum(t for _, t in self._tok_events[provider])
            out[provider] = {
                "rpm_limit": limits.rpm,
                "tpm_limit": limits.tpm,
                "rpm_used": rpm_used,
                "tpm_used": tpm_used,
                "rpm_headroom": max(0.0, min(1.0, (limits.rpm - rpm_used) / max(1, limits.rpm))),
                "tpm_headroom": max(0.0, min(1.0, (limits.tpm - tpm_used) / max(1, limits.tpm))),
            }
        return out

    def _prune(self, provider: str, now: float) -> None:
        cutoff = now - 60.0
        reqs = self._req_events[provider]
        toks = self._tok_events[provider]
        while reqs and reqs[0] < cutoff:
            reqs.popleft()
        while toks and toks[0][0] < cutoff:
            toks.popleft()

    def _next_wait_seconds(self, provider: str, now: float) -> float:
        waits = []
        reqs = self._req_events[provider]
        toks = self._tok_events[provider]
        if reqs:
            waits.append(max(0.0, reqs[0] + 60.0 - now))
        if toks:
            waits.append(max(0.0, toks[0][0] + 60.0 - now))
        return min(waits) if waits else 0.05
