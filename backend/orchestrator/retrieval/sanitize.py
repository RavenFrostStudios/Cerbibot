from __future__ import annotations

import re
from html import unescape

from orchestrator.security.scanners import PROMPT_INJECTION_PATTERNS

UNTRUSTED_BEGIN = "UNTRUSTED_SOURCE_BEGIN"
UNTRUSTED_END = "UNTRUSTED_SOURCE_END"


def html_to_text(html: str) -> str:
    no_script = re.sub(r"<script\b[^<]*(?:(?!</script>)<[^<]*)*</script>", "", html, flags=re.IGNORECASE)
    no_style = re.sub(r"<style\b[^<]*(?:(?!</style>)<[^<]*)*</style>", "", no_script, flags=re.IGNORECASE)
    no_tags = re.sub(r"<[^>]+>", " ", no_style)
    normalized = re.sub(r"\s+", " ", unescape(no_tags)).strip()
    return normalized


def sanitize_retrieved_text(text: str, max_chars: int = 50_000) -> str:
    clean = text
    for pattern in PROMPT_INJECTION_PATTERNS:
        clean = pattern.sub("[REMOVED_INJECTION_PATTERN]", clean)
    clean = clean[:max_chars]
    return clean


def wrap_untrusted_source(text: str) -> str:
    return f"{UNTRUSTED_BEGIN}\n{text}\n{UNTRUSTED_END}"
