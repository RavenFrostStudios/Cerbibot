from __future__ import annotations

from dataclasses import dataclass
import re


_STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "that",
    "this",
    "from",
    "into",
    "just",
    "only",
    "tool",
    "tools",
    "use",
    "run",
    "execute",
}


@dataclass(slots=True)
class IntentDriftResult:
    drifted: bool
    score: float
    overlap: list[str]
    reason: str


def detect_intent_drift(
    *,
    query: str,
    tools_directive: str,
    tool_name: str,
    tool_reason: str,
    tool_args: dict[str, str],
    min_overlap_score: float = 0.1,
) -> IntentDriftResult:
    user_terms = _terms(f"{query}\n{tools_directive}")
    plan_terms = _terms(f"{tool_name}\n{tool_reason}\n{_flatten_args(tool_args)}")

    if not user_terms:
        return IntentDriftResult(drifted=False, score=1.0, overlap=[], reason="no_user_terms")
    if not plan_terms:
        return IntentDriftResult(drifted=True, score=0.0, overlap=[], reason="empty_tool_plan")

    overlap = sorted(user_terms.intersection(plan_terms))
    denom = max(1, min(len(user_terms), len(plan_terms)))
    score = len(overlap) / denom
    drifted = score < min_overlap_score
    reason = "low_semantic_overlap" if drifted else "ok"
    return IntentDriftResult(
        drifted=drifted,
        score=score,
        overlap=overlap,
        reason=reason,
    )


def detect_executor_intent_drift(
    *,
    objective: str,
    executor_cmd: str,
    requested_files: list[str] | None = None,
    checks: list[str] | None = None,
    min_overlap_score: float = 0.1,
) -> IntentDriftResult:
    intent_terms = _terms(
        " ".join(
            [
                objective,
                " ".join(requested_files or []),
                " ".join(checks or []),
            ]
        )
    )
    plan_terms = _terms(executor_cmd)
    return _score_overlap(intent_terms, plan_terms, min_overlap_score=min_overlap_score)


def detect_diff_intent_drift(
    *,
    objective: str,
    changed_files: list[str],
    requested_files: list[str] | None = None,
    min_overlap_score: float = 0.05,
) -> IntentDriftResult:
    intent_terms = _terms(" ".join([objective, " ".join(requested_files or [])]))
    file_terms = _terms(" ".join(changed_files))
    return _score_overlap(intent_terms, file_terms, min_overlap_score=min_overlap_score)


def _score_overlap(user_terms: set[str], plan_terms: set[str], *, min_overlap_score: float) -> IntentDriftResult:
    if not user_terms:
        return IntentDriftResult(drifted=False, score=1.0, overlap=[], reason="no_user_terms")
    if not plan_terms:
        return IntentDriftResult(drifted=True, score=0.0, overlap=[], reason="empty_tool_plan")
    overlap = sorted(user_terms.intersection(plan_terms))
    denom = max(1, min(len(user_terms), len(plan_terms)))
    score = len(overlap) / denom
    drifted = score < min_overlap_score
    reason = "low_semantic_overlap" if drifted else "ok"
    return IntentDriftResult(drifted=drifted, score=score, overlap=overlap, reason=reason)


def _terms(text: str) -> set[str]:
    raw = re.findall(r"[a-zA-Z0-9_]{3,}", text.lower())
    return {token for token in raw if token not in _STOPWORDS}


def _flatten_args(args: dict[str, str]) -> str:
    parts: list[str] = []
    for key, value in sorted(args.items()):
        parts.append(str(key))
        parts.append(str(value))
    return " ".join(parts)
