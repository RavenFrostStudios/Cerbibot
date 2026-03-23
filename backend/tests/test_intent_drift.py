from __future__ import annotations

from orchestrator.security.intent_drift import (
    detect_diff_intent_drift,
    detect_executor_intent_drift,
    detect_intent_drift,
)


def test_detect_intent_drift_flags_mismatch() -> None:
    result = detect_intent_drift(
        query="Summarize security headlines",
        tools_directive="use web retrieval only",
        tool_name="python_exec",
        tool_reason="list local filesystem",
        tool_args={"code": "import os; print(os.listdir('/'))"},
    )
    assert result.drifted is True
    assert result.score < 0.1


def test_detect_intent_drift_allows_aligned_plan() -> None:
    result = detect_intent_drift(
        query="Calculate 8*7",
        tools_directive="run python code",
        tool_name="python_exec",
        tool_reason="needs computation",
        tool_args={"code": "print(8*7)"},
    )
    assert result.drifted is False


def test_detect_executor_intent_drift_flags_mismatch() -> None:
    result = detect_executor_intent_drift(
        objective="update readme docs",
        executor_cmd="rm -rf /tmp/build-cache",
        requested_files=["README.md"],
        checks=["test -f README.md"],
    )
    assert result.drifted is True


def test_detect_diff_intent_drift_allows_related_changes() -> None:
    result = detect_diff_intent_drift(
        objective="update readme docs",
        changed_files=["README.md", "docs/overview.md"],
        requested_files=[],
    )
    assert result.drifted is False
