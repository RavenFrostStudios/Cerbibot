from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Callable

from orchestrator.security.scanners import (
    API_KEY_PATTERNS,
    CARD_PATTERN,
    EMAIL_PATTERN,
    JWT_PATTERN,
    PHONE_PATTERN,
    SSN_PATTERN,
    is_probable_card_number,
)


@dataclass(slots=True)
class PrivacyMaskResult:
    masked_text: str
    mapping: dict[str, str]
    counts: dict[str, int]


def mask_sensitive_text(text: str) -> PrivacyMaskResult:
    spans: list[tuple[int, int, str, str]] = []
    _collect(spans, "SECRET", API_KEY_PATTERNS, text)
    _collect(spans, "JWT", [JWT_PATTERN], text)
    _collect(spans, "EMAIL", [EMAIL_PATTERN], text)
    _collect(spans, "PHONE", [PHONE_PATTERN], text)
    _collect(spans, "SSN", [SSN_PATTERN], text)
    _collect(spans, "CARD", [CARD_PATTERN], text, value_validator=is_probable_card_number)

    if not spans:
        return PrivacyMaskResult(masked_text=text, mapping={}, counts={})

    spans.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    filtered: list[tuple[int, int, str, str]] = []
    cursor = -1
    for start, end, label, value in spans:
        if start < cursor:
            continue
        filtered.append((start, end, label, value))
        cursor = end

    label_counts: dict[str, int] = {}
    mapping: dict[str, str] = {}
    chunks: list[str] = []
    at = 0
    for start, end, label, value in filtered:
        chunks.append(text[at:start])
        idx = label_counts.get(label, 0) + 1
        label_counts[label] = idx
        token = f"[MASK_{label}_{idx}]"
        mapping[token] = value
        chunks.append(token)
        at = end
    chunks.append(text[at:])
    return PrivacyMaskResult(masked_text="".join(chunks), mapping=mapping, counts=label_counts)


def rehydrate_text(text: str, mapping: dict[str, str]) -> str:
    out = text
    for token, original in mapping.items():
        out = out.replace(token, original)
    return out


def _collect(
    spans: list[tuple[int, int, str, str]],
    label: str,
    patterns: list[re.Pattern[str]],
    text: str,
    *,
    value_validator: Callable[[str], bool] | None = None,
) -> None:
    for pattern in patterns:
        for match in pattern.finditer(text):
            value = match.group(0)
            if value_validator is not None and not value_validator(value):
                continue
            spans.append((match.start(), match.end(), label, value))
