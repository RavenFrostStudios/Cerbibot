from __future__ import annotations

from orchestrator.security.scanners import scan_text


def redact_text(text: str) -> str:
    """Replace detected secrets/PII with typed placeholders."""
    redacted = text
    for finding in scan_text(text):
        placeholder = f"[REDACTED_{finding.category.upper()}]"
        redacted = redacted.replace(finding.value, placeholder)
    return redacted
