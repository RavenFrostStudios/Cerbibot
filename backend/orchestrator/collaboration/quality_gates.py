from __future__ import annotations

import json
import re


_PLACEHOLDER_PATTERNS = (
    "instructions received. ready for query.",
    "ready for query",
    "ready for queries",
    "ready when you are",
    "no substantive query provided",
    "no specific query provided",
    "no query provided",
    "please provide the query",
    "please provide query",
    "awaiting query",
    "awaiting your query",
    "ready. send your question or task",
    "send your question or task",
    "ready for your question",
    "ready for your prompt",
    "ready for your task",
    "ready for your request",
    "ready to assist",
    "is ready to assist",
    "cerbibot is ready to assist",
    "ready to help",
    "cerbibot here, ready to help",
    "no response content was generated. please retry.",
    "no original response provided",
    "no original response was provided",
)

_META_REVIEW_PATTERNS = (
    "the original response",
    "response is truncated",
    "overall, technically sound",
    "minor caveats",
    "partially accurate review",
    "lacks the 5th security control",
    "critique notes",
)

_POLICY_REFUSAL_PATTERNS = (
    "i must decline",
    "i have to decline",
    "i can't help with that",
    "i cannot help with that",
    "cannot assist with that",
    "appears to be an attempt to circumvent",
    "circumvent my guidelines",
    "violates my guidelines",
    "violates policy",
)

_HIGH_RISK_QUERY_MARKERS = (
    "malware",
    "ransomware",
    "exploit",
    "phishing",
    "ddos",
    "botnet",
    "sql injection",
    "xss",
    "bypass auth",
    "credential stuffing",
    "make a bomb",
    "weapon",
)


def is_placeholder_response(text: str) -> bool:
    normalized = str(text or "").strip().lower()
    if not normalized:
        return True
    if normalized.startswith("no response content was generated"):
        return True
    if normalized.startswith("[critique notes]"):
        return True
    if "ready for quer" in normalized:
        return True
    if "ready for your question" in normalized:
        return True
    if "ready to assist" in normalized:
        return True
    if "ready to help" in normalized:
        return True
    if normalized.startswith("acknowledged") and "ready" in normalized:
        return True
    return any(pattern in normalized for pattern in _PLACEHOLDER_PATTERNS)


def is_meta_review_response(text: str) -> bool:
    normalized = str(text or "").strip().lower()
    if not normalized:
        return False
    if normalized.startswith("{") and normalized.endswith("}"):
        try:
            payload = json.loads(normalized)
        except Exception:
            payload = None
        if isinstance(payload, dict):
            keys = {str(key).strip().lower() for key in payload.keys()}
            if {"answer", "claims", "assumptions", "evidence_needed"}.issubset(keys):
                return True
    return any(pattern in normalized for pattern in _META_REVIEW_PATTERNS)


def is_policy_refusal_response(text: str) -> bool:
    normalized = str(text or "").strip().lower()
    if not normalized:
        return False
    return any(pattern in normalized for pattern in _POLICY_REFUSAL_PATTERNS)


def is_high_risk_query(query: str) -> bool:
    normalized = str(query or "").strip().lower()
    return any(marker in normalized for marker in _HIGH_RISK_QUERY_MARKERS)


def is_low_signal_final_answer(text: str) -> bool:
    normalized = str(text or "").strip().lower()
    if not normalized:
        return True
    if normalized.startswith("[critique notes]"):
        return True
    return (
        is_placeholder_response(text)
        or is_policy_refusal_response(text)
        or is_meta_review_response(text)
    )


def score_answer_quality(
    text: str,
    *,
    user_query: str = "",
    min_words: int = 50,
) -> tuple[float, list[str]]:
    """Return a lightweight semantic quality score in [0, 1] and reasons.

    This is intentionally heuristic and cheap:
    - catches greeting/meta/truncated low-content responses as a class
    - checks rough topical overlap with user query
    """
    normalized = str(text or "").strip()
    if not normalized:
        return 0.0, ["empty_output"]
    if is_placeholder_response(normalized):
        return 0.0, ["placeholder_output"]
    if is_policy_refusal_response(normalized):
        return 0.0, ["policy_refusal_output"]
    if is_meta_review_response(normalized):
        return 0.0, ["meta_review_output"]

    reasons: list[str] = []
    score = 1.0

    words = re.findall(r"[A-Za-z0-9_'-]+", normalized)
    if len(words) < min_words:
        score -= 0.45
        reasons.append("too_short")

    lowered = normalized.lower()
    if lowered.endswith(":") or lowered.endswith("..."):
        score -= 0.2
        reasons.append("possibly_truncated")
    if "this plan" in lowered and len(words) < 80:
        score -= 0.2
        reasons.append("intro_only_without_substance")
    if ("i'm" in lowered or "i am" in lowered or "here to help" in lowered) and len(words) < 90:
        score -= 0.2
        reasons.append("meta_intro_heavy")

    if user_query.strip():
        query_tokens = {
            tok
            for tok in re.findall(r"[a-z0-9_]+", user_query.lower())
            if len(tok) > 3 and tok not in {"what", "with", "then", "this", "that", "your", "from", "into", "have"}
        }
        if query_tokens:
            answer_tokens = set(re.findall(r"[a-z0-9_]+", lowered))
            overlap = len(query_tokens.intersection(answer_tokens)) / max(1, len(query_tokens))
            if overlap < 0.18:
                score -= 0.25
                reasons.append("low_query_overlap")

    return max(0.0, min(1.0, score)), reasons


def is_low_signal_by_quality(
    text: str,
    *,
    user_query: str = "",
    threshold: float = 0.55,
) -> bool:
    score, _ = score_answer_quality(text, user_query=user_query)
    return score < threshold
