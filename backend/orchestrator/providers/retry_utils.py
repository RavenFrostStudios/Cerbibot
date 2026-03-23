from __future__ import annotations

import random


def extract_status_code(err: Exception) -> int | None:
    status = getattr(err, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(err, "response", None)
    response_status = getattr(response, "status_code", None)
    if isinstance(response_status, int):
        return response_status
    return None


def is_retryable_status_code(status_code: int) -> bool:
    if status_code in (408, 409, 425, 429):
        return True
    return status_code >= 500


def is_retryable_error(err: Exception, *, retryable_types: tuple[type[Exception], ...] = ()) -> bool:
    status_code = extract_status_code(err)
    if status_code is not None:
        return is_retryable_status_code(status_code)

    if retryable_types and isinstance(err, retryable_types):
        return True

    msg = str(err).lower()
    transient_markers = (
        "timeout",
        "timed out",
        "deadline exceeded",
        "temporarily unavailable",
        "service unavailable",
        "connection reset",
        "connection aborted",
        "connection error",
        "too many requests",
        "rate limit",
        "overloaded",
        "try again",
    )
    return any(marker in msg for marker in transient_markers)


def retry_backoff_seconds(
    attempt_index: int,
    *,
    base_seconds: float = 1.0,
    jitter_seconds: float = 0.3,
    max_seconds: float = 8.0,
) -> float:
    exponential = base_seconds * (2**max(0, attempt_index))
    jitter = random.uniform(0.0, max(0.0, jitter_seconds))
    return min(max_seconds, exponential + jitter)
