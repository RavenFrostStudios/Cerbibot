from __future__ import annotations

import re


def summarize_for_memory(text: str, *, max_chars: int = 320) -> str:
    clean = re.sub(r"\s+", " ", text).strip()
    if not clean:
        return ""
    if len(clean) <= max_chars:
        return clean
    # Keep a compact summary by sentence-ish chunks before hard truncation.
    parts = re.split(r"(?<=[.!?])\s+", clean)
    summary = ""
    for part in parts:
        candidate = (summary + " " + part).strip()
        if len(candidate) > max_chars:
            break
        summary = candidate
    if not summary:
        summary = clean[: max_chars - 3].rstrip() + "..."
    return summary
