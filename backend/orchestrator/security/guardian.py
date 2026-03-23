from __future__ import annotations

from dataclasses import dataclass

from orchestrator.config import SecurityConfig
from orchestrator.observability.redaction import redact_text
from orchestrator.security.scanners import scan_text
from orchestrator.security.taint import TaintedString
from orchestrator.security.taint_validator import validate_tool_args


@dataclass(slots=True)
class GuardianResult:
    passed: bool
    flags: list[str]
    redacted_text: str


class Guardian:
    """Runs deterministic security scans at pre-flight and post-output stages."""

    def __init__(self, config: SecurityConfig):
        self.config = config

    def preflight(self, text: str) -> GuardianResult:
        findings = scan_text(text)
        flags = sorted({f.category for f in findings})
        redacted = redact_text(text)
        blocked_categories = {"secret", "pii_ssn", "pii_card"}
        passed = not (self.config.block_on_secrets and blocked_categories.intersection(flags))
        return GuardianResult(passed=passed, flags=flags, redacted_text=redacted)

    def post_output(self, text: str) -> GuardianResult:
        findings = scan_text(text)
        flags = sorted({f.category for f in findings})
        redacted = redact_text(text)
        passed = "secret" not in flags
        return GuardianResult(passed=passed, flags=flags, redacted_text=redacted)

    def validate_tool_arguments(
        self,
        tool_name: str,
        args: dict[str, TaintedString | str],
        *,
        path_allow_prefixes: list[str] | None = None,
        arg_patterns: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """Validate and strip taint from tool arguments using policy constraints."""
        return validate_tool_args(
            tool_name=tool_name,
            args=args,
            url_allowlist=self.config.retrieval_domain_allowlist or None,
            url_denylist=self.config.retrieval_domain_denylist or None,
            path_allow_prefixes=path_allow_prefixes or [],
            arg_patterns=arg_patterns or None,
        )
