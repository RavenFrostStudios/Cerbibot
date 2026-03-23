from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from orchestrator.observability.redaction import redact_text
from orchestrator.security.guardian import Guardian
from orchestrator.security.scanners import scan_text


@dataclass(slots=True)
class MemoryDecision:
    allowed: bool
    reason: str
    redacted_statement: str


class MemoryGovernance:
    def __init__(self, guardian: Guardian):
        self.guardian = guardian

    def evaluate_write(
        self,
        *,
        statement: str,
        source_type: str,
        source_ref: str,
        is_model_inferred: bool,
        confirm_fn: Callable[[str], bool] | None = None,
    ) -> MemoryDecision:
        _ = source_type, source_ref
        scanned = scan_text(statement)
        blocked_categories = {"secret", "pii_ssn", "pii_card"}
        found = {item.category for item in scanned}
        redacted = redact_text(statement)

        if blocked_categories.intersection(found):
            return MemoryDecision(
                allowed=False,
                reason=f"blocked_categories={sorted(blocked_categories.intersection(found))}",
                redacted_statement=redacted,
            )

        preflight = self.guardian.preflight(statement)
        if not preflight.passed:
            return MemoryDecision(
                allowed=False,
                reason=f"guardian_preflight_failed={preflight.flags}",
                redacted_statement=preflight.redacted_text,
            )

        if is_model_inferred:
            if confirm_fn is None:
                return MemoryDecision(
                    allowed=False,
                    reason="model_inferred_requires_confirmation",
                    redacted_statement=preflight.redacted_text,
                )
            approved = confirm_fn("Store model-inferred memory? This may persist unverified information.")
            if not approved:
                return MemoryDecision(
                    allowed=False,
                    reason="user_denied_model_inferred_memory",
                    redacted_statement=preflight.redacted_text,
                )

        return MemoryDecision(allowed=True, reason="ok", redacted_statement=preflight.redacted_text)
