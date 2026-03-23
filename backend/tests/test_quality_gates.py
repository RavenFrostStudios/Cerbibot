from __future__ import annotations

from orchestrator.collaboration.quality_gates import is_low_signal_by_quality, score_answer_quality


def test_quality_gate_flags_short_meta_intro() -> None:
    text = "CerbiBot here, ready to help design your billing engine rollout!"
    score, reasons = score_answer_quality(
        text,
        user_query="Design a production rollout plan for a new billing engine in a SaaS app.",
    )
    assert score < 0.55
    assert reasons
    assert is_low_signal_by_quality(
        text,
        user_query="Design a production rollout plan for a new billing engine in a SaaS app.",
    )


def test_quality_gate_accepts_substantive_on_topic_answer() -> None:
    text = (
        "Days 1-3: deploy behind flags and run shadow billing with reconciliation. "
        "Days 4-8: canary 1% then 5% with auto rollback on error, latency, and revenue deltas. "
        "Days 9-15: ramp to 25% and 50% by tenant cohorts while excluding top customers until "
        "accuracy and support ticket metrics remain stable. Days 16-24: expand to 75% then 100% "
        "with blue/green fallback and daily finance audits. Days 25-30: stabilize, postmortem, "
        "remove temporary flags, and finalize SLOs for billing operations."
    )
    score, _ = score_answer_quality(
        text,
        user_query="Design a production rollout plan for a new billing engine in a SaaS app.",
    )
    assert score >= 0.55
    assert not is_low_signal_by_quality(
        text,
        user_query="Design a production rollout plan for a new billing engine in a SaaS app.",
    )
