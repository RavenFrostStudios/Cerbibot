from __future__ import annotations

import pytest

from orchestrator.rate_limiter import ProviderRateLimits, RateLimitExceededError, RateLimiter


@pytest.mark.asyncio
async def test_rate_limiter_blocks_when_wait_exceeds_max() -> None:
    limiter = RateLimiter({"openai": ProviderRateLimits(rpm=1, tpm=1000, max_wait_seconds=0.01)})
    await limiter.acquire("openai", tokens=10)
    with pytest.raises(RateLimitExceededError):
        await limiter.acquire("openai", tokens=10)


@pytest.mark.asyncio
async def test_rate_limiter_headroom_and_snapshot() -> None:
    limiter = RateLimiter({"openai": ProviderRateLimits(rpm=3, tpm=300, max_wait_seconds=1.0)})
    await limiter.acquire("openai", tokens=100)
    hr = limiter.headroom("openai")
    assert 0.0 <= hr["rpm_headroom"] < 1.0
    assert 0.0 <= hr["tpm_headroom"] < 1.0

    snap = limiter.snapshot()
    assert "openai" in snap
    assert snap["openai"]["rpm_limit"] == 3
    assert snap["openai"]["tpm_limit"] == 300
    assert snap["openai"]["rpm_used"] == 1
    assert snap["openai"]["tpm_used"] == 100
