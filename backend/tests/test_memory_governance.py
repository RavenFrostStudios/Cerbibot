from __future__ import annotations

from orchestrator.config import SecurityConfig
from orchestrator.memory.governance import MemoryGovernance
from orchestrator.security.guardian import Guardian


def _governance() -> MemoryGovernance:
    guardian = Guardian(
        SecurityConfig(
            block_on_secrets=True,
            redact_logs=True,
            tool_allowlist=[],
            retrieval_domain_allowlist=[],
            retrieval_domain_denylist=[],
        )
    )
    return MemoryGovernance(guardian)


def test_governance_blocks_secret_like_data() -> None:
    decision = _governance().evaluate_write(
        statement="my key is sk-abcdefghijklmnopqrstu",
        source_type="summary",
        source_ref="run:1",
        is_model_inferred=False,
    )
    assert decision.allowed is False


def test_governance_requires_confirmation_for_model_inferred() -> None:
    decision = _governance().evaluate_write(
        statement="User likes Python",
        source_type="summary",
        source_ref="run:1",
        is_model_inferred=True,
        confirm_fn=lambda _prompt: False,
    )
    assert decision.allowed is False
    assert "denied" in decision.reason


def test_governance_allows_safe_statement() -> None:
    decision = _governance().evaluate_write(
        statement="User prefers unit tests before refactors.",
        source_type="user_preference",
        source_ref="manual",
        is_model_inferred=False,
    )
    assert decision.allowed is True
