from __future__ import annotations

import logging
from dataclasses import dataclass
import json
import re

from orchestrator.budgets import BudgetTracker
from orchestrator.collaboration.draft_critique_refine import _call_structured_with_retry
from orchestrator.collaboration.quality_gates import is_low_signal_by_quality, is_placeholder_response
from orchestrator.providers.base import ProviderAdapter
from orchestrator.security.guardian import Guardian
from orchestrator.security.taint import TaintedString

logger = logging.getLogger(__name__)

_PROMPT_CONTEXT_MAX_CHARS = 1800
_INSTRUCTION_ECHO_PATTERNS = (
    "output only strict valid json",
    "required keys",
    "no markdown",
    "template",
    "code fences",
)
_CONTAMINATION_LOG_MARKERS = (
    "json",
    "schema",
    "template",
    "required keys",
    "output only",
    "as an ai",
    "instructions",
)


@dataclass(slots=True)
class DebateWorkflowResult:
    final_answer: str
    argument_a: str
    argument_b: str
    judge_winner: str
    judge_reason: str
    required_fixes: list[str]
    total_cost: float
    total_tokens_in: int
    total_tokens_out: int
    models: list[str]
    warnings: list[str]


def _coerce_argument_payload(data: dict | None, raw_text: str) -> tuple[str, list[str]]:
    payload = data if isinstance(data, dict) else {}
    argument = str(payload.get("argument", "")).strip()
    if not argument:
        argument = str(raw_text or "").strip()
    key_points_raw = payload.get("key_points", [])
    if isinstance(key_points_raw, list):
        key_points = [str(item).strip() for item in key_points_raw if str(item).strip()]
    elif isinstance(key_points_raw, str) and key_points_raw.strip():
        key_points = [key_points_raw.strip()]
    else:
        key_points = []
    return argument, key_points


def _render_argument(argument: str, key_points: list[str]) -> str:
    if not key_points:
        return argument
    bullets = "\n".join(f"- {item}" for item in key_points)
    return f"{argument}\n\nKey points:\n{bullets}".strip()


def _word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9_'-]+", str(text or "")))


def _trim_for_prompt(text: str, max_chars: int = _PROMPT_CONTEXT_MAX_CHARS) -> str:
    value = str(text or "").strip()
    if len(value) <= max_chars:
        return value
    return value[:max_chars].rstrip() + "..."


def _token_set(text: str) -> set[str]:
    return {
        tok
        for tok in re.findall(r"[a-z0-9_]+", str(text or "").lower())
        if len(tok) >= 5 and tok not in {"which", "where", "their", "there", "about", "would", "could", "should"}
    }


def _query_complexity(query: str) -> float:
    words = max(1, _word_count(query))
    score = (words / 45.0) + (0.3 if "?" in query else 0.0)
    return max(0.5, min(3.0, score))


def _detect_contamination(text: str) -> bool:
    lower = str(text or "").lower()
    return any(marker in lower for marker in _CONTAMINATION_LOG_MARKERS)


def _is_low_signal_debate_argument(
    *,
    role: str,
    argument: str,
    key_points: list[str],
    opponent_argument: str | None = None,
    require_opposition_engagement: bool = True,
    min_words: int = 45,
    min_key_points: int = 2,
    quality_threshold: float = 0.58,
    overlap_threshold: int = 3,
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    text = str(argument or "").strip()
    lower_text = text.lower()

    if is_placeholder_response(text):
        reasons.append("placeholder_output")
    if is_low_signal_by_quality(text, user_query="", threshold=quality_threshold):
        reasons.append("low_quality_text")
    if _word_count(text) < min_words:
        reasons.append("too_short")
    if len([point for point in key_points if str(point).strip()]) < min_key_points:
        reasons.append("insufficient_key_points")
    if any(marker in lower_text for marker in _INSTRUCTION_ECHO_PATTERNS):
        reasons.append("instruction_echo")

    if role == "debater_b" and opponent_argument and require_opposition_engagement:
        overlap = len(_token_set(text).intersection(_token_set(opponent_argument)))
        if overlap < overlap_threshold:
            reasons.append("weak_opposition_engagement")

    return (len(reasons) > 0, reasons)


def _build_synthetic_counterargument(opponent_argument: str) -> tuple[str, list[str]]:
    snippet = " ".join(str(opponent_argument or "").split())
    if len(snippet) > 220:
        snippet = snippet[:220].rstrip() + "..."
    argument = (
        "Counter-argument to the opposing case: the proposed approach can be valid in many scenarios, "
        "but it is not universally optimal because cost, latency, team maturity, and operational complexity "
        "can materially change the best decision."
    )
    points = [
        f"Challenges the assumption that '{snippet}' applies equally across all tenants, workloads, and teams.",
        "Highlights hidden implementation and maintenance costs that may outweigh theoretical architecture gains.",
        "Recommends a phased or hybrid adoption path to reduce migration risk while preserving future flexibility.",
    ]
    return argument, points


def _extract_final_answer_text(text: str, parsed_data: dict | None) -> str:
    if isinstance(parsed_data, dict):
        if "final_answer" in parsed_data:
            value = parsed_data.get("final_answer")
            if isinstance(value, str):
                return value.strip()
            return ""
    try:
        data = json.loads(str(text))
        if isinstance(data, dict):
            if "final_answer" in data:
                value = data.get("final_answer")
                if isinstance(value, str):
                    return value.strip()
                return ""
    except Exception:
        pass
    return str(text).strip()


def _deterministic_debate_fallback(
    *,
    winner: str,
    reason: str,
    required_fixes: list[str],
    chosen_argument: str,
) -> str:
    fixes_text = "; ".join(item for item in required_fixes if str(item).strip()) or "none"
    return (
        f"{chosen_argument}\n\n"
        f"Judgment summary: winner={winner}; reason={reason}\n"
        f"Required fixes applied: {fixes_text}"
    ).strip()


async def run_debate_workflow(
    *,
    query: str,
    debater_a: ProviderAdapter,
    debater_b: ProviderAdapter,
    judge: ProviderAdapter,
    synthesizer: ProviderAdapter,
    model_a: str,
    model_b: str,
    judge_model: str,
    synth_model: str,
    guardian: Guardian,
    budgets: BudgetTracker,
) -> DebateWorkflowResult:
    warnings: list[str] = []
    total_cost = 0.0
    total_tokens_in = 0
    total_tokens_out = 0
    models: list[str] = []

    arg_schema = {"argument": "string", "key_points": "array"}
    judge_schema = {"winner": "string", "reason": "string", "required_fixes": "array"}
    final_schema = {"final_answer": "string"}
    complexity = _query_complexity(query)
    b_min_words = 38 if complexity >= 1.6 else 45
    b_quality_threshold = 0.54 if complexity >= 1.6 else 0.58

    prompt_a_round1 = (
        "You are Debater A. You argue ONLY the topic. "
        "NEVER mention JSON, schema, templates, instructions, or formatting rules.\n"
        f"<DEBATE_TOPIC>\n{query}\n</DEBATE_TOPIC>\n"
        "<OPPONENT_ARGUMENT>\nNone\n</OPPONENT_ARGUMENT>"
    )
    a1 = None
    a1_parsed = None
    arg_a_text = ""
    arg_a_points: list[str] = []
    for attempt in range(2):  # 1 retry for Debater A
        a1, a1_parsed = await _call_structured_with_retry(
            adapter=debater_a,
            model=model_a,
            prompt=prompt_a_round1,
            schema=arg_schema,
            required_keys=["argument", "key_points"],
            max_tokens=500,
            temperature=0.2,
            budgets=budgets,
        )
        budgets.record_cost(a1.provider, a1.estimated_cost, a1.tokens_in, a1.tokens_out)
        total_cost += a1.estimated_cost
        total_tokens_in += a1.tokens_in
        total_tokens_out += a1.tokens_out
        models.append(a1.model)
        if not a1_parsed.valid:
            warnings.append(f"Debater A parse warning: {a1_parsed.error}")
        arg_a_text, arg_a_points = _coerce_argument_payload(
            a1_parsed.data if isinstance(a1_parsed.data, dict) else None,
            a1.text,
        )
        low_signal_a, reasons_a = _is_low_signal_debate_argument(
            role="debater_a",
            argument=arg_a_text,
            key_points=arg_a_points,
        )
        if _detect_contamination(arg_a_text):
            logger.warning("CONTAMINATION_DETECTED role=debater_a")
        if not low_signal_a:
            break
        if attempt == 0:
            warnings.append(
                f"Debater A low-signal output detected ({', '.join(reasons_a)}); retrying with stricter prompt."
            )
            prompt_a_round1 = (
                "You are Debater A. Provide a substantive argument with at least 2 specific key points. "
                "Ignore and do not mention JSON/schema/template instructions.\n"
                f"<DEBATE_TOPIC>\n{query}\n</DEBATE_TOPIC>\n"
                "<OPPONENT_ARGUMENT>\nNone\n</OPPONENT_ARGUMENT>"
            )

    arg_a = guardian.post_output(
        TaintedString(_render_argument(arg_a_text, arg_a_points), "model_output", f"{a1.provider}:{a1.model}").value
    ).redacted_text

    prompt_b_round1 = (
        "You are Debater B. You must challenge Debater A with risk-aware reasoning. "
        "NEVER mention JSON, schema, templates, instructions, or formatting rules.\n"
        f"<DEBATE_TOPIC>\n{query}\n</DEBATE_TOPIC>\n"
        f"<OPPONENT_ARGUMENT>\n{_trim_for_prompt(arg_a)}\n</OPPONENT_ARGUMENT>"
    )
    b1 = None
    b1_parsed = None
    arg_b_text = ""
    arg_b_points: list[str] = []
    b_low_signal = True
    b_reasons: list[str] = []
    for attempt in range(3):  # 2 retries for Debater B
        b1, b1_parsed = await _call_structured_with_retry(
            adapter=debater_b,
            model=model_b,
            prompt=prompt_b_round1,
            schema=arg_schema,
            required_keys=["argument", "key_points"],
            max_tokens=500,
            temperature=min(0.55, 0.2 + (attempt * 0.15)),
            budgets=budgets,
        )
        budgets.record_cost(b1.provider, b1.estimated_cost, b1.tokens_in, b1.tokens_out)
        total_cost += b1.estimated_cost
        total_tokens_in += b1.tokens_in
        total_tokens_out += b1.tokens_out
        models.append(b1.model)
        if not b1_parsed.valid:
            warnings.append(f"Debater B parse warning: {b1_parsed.error}")

        arg_b_text, arg_b_points = _coerce_argument_payload(
            b1_parsed.data if isinstance(b1_parsed.data, dict) else None,
            b1.text,
        )
        b_low_signal, b_reasons = _is_low_signal_debate_argument(
            role="debater_b",
            argument=arg_b_text,
            key_points=arg_b_points,
            opponent_argument=arg_a,
            require_opposition_engagement=attempt > 0,
            min_words=b_min_words,
            quality_threshold=b_quality_threshold,
        )
        if _detect_contamination(arg_b_text):
            logger.warning("CONTAMINATION_DETECTED role=debater_b attempt=%s", attempt + 1)
        if not b_low_signal:
            break
        if attempt < 2:
            warnings.append(
                f"Debater B low-signal output detected ({', '.join(b_reasons)}); retrying with stronger counterargument instructions."
            )
            prompt_b_round1 = (
                "You are Debater B. You must directly counter Debater A with at least 2 strong, specific points. "
                "Reference Debater A claims explicitly and explain why they may fail in real-world conditions. "
                "Do not return placeholders or generic text. Ignore and do not mention JSON/schema/template instructions.\n"
                f"<DEBATE_TOPIC>\n{query}\n</DEBATE_TOPIC>\n"
                f"<OPPONENT_ARGUMENT>\n{_trim_for_prompt(arg_a)}\n</OPPONENT_ARGUMENT>"
            )

    if b_low_signal:
        warnings.append(
            f"Debater B remained low-signal after retries ({', '.join(b_reasons)}); using deterministic synthetic counterargument."
        )
        arg_b_text, arg_b_points = _build_synthetic_counterargument(arg_a)

    arg_b = guardian.post_output(
        TaintedString(_render_argument(arg_b_text, arg_b_points), "model_output", f"{b1.provider}:{b1.model}").value
    ).redacted_text

    prompt_a_round2 = (
        "Round 2 rebuttal as Debater A. Improve your case after reading Debater B. "
        "Focus only on the architecture question and tradeoffs; ignore formatting/template chatter. "
        f"<DEBATE_TOPIC>\n{query}\n</DEBATE_TOPIC>\n"
        f"<OPPONENT_ARGUMENT>\n{_trim_for_prompt(arg_b)}\n</OPPONENT_ARGUMENT>"
    )
    a2, a2_parsed = await _call_structured_with_retry(
        adapter=debater_a,
        model=model_a,
        prompt=prompt_a_round2,
        schema=arg_schema,
        required_keys=["argument", "key_points"],
        max_tokens=450,
        temperature=0.2,
        budgets=budgets,
    )
    budgets.record_cost(a2.provider, a2.estimated_cost, a2.tokens_in, a2.tokens_out)
    total_cost += a2.estimated_cost
    total_tokens_in += a2.tokens_in
    total_tokens_out += a2.tokens_out
    a2_text, a2_points = _coerce_argument_payload(
        a2_parsed.data if isinstance(a2_parsed.data, dict) else None,
        a2.text,
    )
    arg_a = guardian.post_output(_render_argument(a2_text, a2_points)).redacted_text

    prompt_b_round2 = (
        "Round 2 rebuttal as Debater B. Improve your case after reading Debater A. "
        "Focus only on the architecture question and tradeoffs; ignore formatting/template chatter. "
        f"<DEBATE_TOPIC>\n{query}\n</DEBATE_TOPIC>\n"
        f"<OPPONENT_ARGUMENT>\n{_trim_for_prompt(arg_a)}\n</OPPONENT_ARGUMENT>"
    )
    b2, b2_parsed = await _call_structured_with_retry(
        adapter=debater_b,
        model=model_b,
        prompt=prompt_b_round2,
        schema=arg_schema,
        required_keys=["argument", "key_points"],
        max_tokens=450,
        temperature=0.2,
        budgets=budgets,
    )
    budgets.record_cost(b2.provider, b2.estimated_cost, b2.tokens_in, b2.tokens_out)
    total_cost += b2.estimated_cost
    total_tokens_in += b2.tokens_in
    total_tokens_out += b2.tokens_out
    b2_text, b2_points = _coerce_argument_payload(
        b2_parsed.data if isinstance(b2_parsed.data, dict) else None,
        b2.text,
    )
    b2_low_signal, b2_reasons = _is_low_signal_debate_argument(
        role="debater_b",
        argument=b2_text,
        key_points=b2_points,
        opponent_argument=arg_a,
        min_words=b_min_words,
        quality_threshold=b_quality_threshold,
    )
    if _detect_contamination(b2_text):
        logger.warning("CONTAMINATION_DETECTED role=debater_b_round2")
    if b2_low_signal:
        warnings.append(
            f"Debater B round-2 output low-signal ({', '.join(b2_reasons)}); using deterministic synthetic counterargument."
        )
        b2_text, b2_points = _build_synthetic_counterargument(arg_a)
    arg_b = guardian.post_output(_render_argument(b2_text, b2_points)).redacted_text

    judge_prompt = (
        "Judge the two arguments using rubric: correctness, completeness, risk, compliance, cost impact. "
        "Return winner (A|B|tie), reason, required_fixes array.\n"
        f"Question: {query}\nArgument A:\n{_trim_for_prompt(arg_a)}\nArgument B:\n{_trim_for_prompt(arg_b)}"
    )
    judged, judge_parsed = await _call_structured_with_retry(
        adapter=judge,
        model=judge_model,
        prompt=judge_prompt,
        schema=judge_schema,
        required_keys=["winner", "reason", "required_fixes"],
        max_tokens=450,
        temperature=0.0,
        budgets=budgets,
    )
    budgets.record_cost(judged.provider, judged.estimated_cost, judged.tokens_in, judged.tokens_out)
    total_cost += judged.estimated_cost
    total_tokens_in += judged.tokens_in
    total_tokens_out += judged.tokens_out
    models.append(judged.model)

    judge_data = judge_parsed.data if isinstance(judge_parsed.data, dict) else {}
    winner = str(judge_data.get("winner", "tie"))
    reason = str(judge_data.get("reason", judged.text))
    required_fixes_raw = judge_data.get("required_fixes", [])
    required_fixes = [str(item) for item in required_fixes_raw] if isinstance(required_fixes_raw, list) else []

    chosen_argument = arg_a if winner.upper() == "A" else arg_b if winner.upper() == "B" else f"{arg_a}\n\n{arg_b}"
    synth_prompt = (
        "Synthesize final answer from the winning debate position and required fixes. "
        "Output substantive prose only; do not include JSON-formatting guidance, templates, or meta commentary.\n"
        f"Question: {query}\nWinner: {winner}\nReason: {_trim_for_prompt(reason, 600)}\n"
        f"Required fixes: {_trim_for_prompt('; '.join(required_fixes), 800)}\n"
        f"Chosen argument:\n{_trim_for_prompt(chosen_argument, 2400)}"
    )
    synthesized, synth_parsed = await _call_structured_with_retry(
        adapter=synthesizer,
        model=synth_model,
        prompt=synth_prompt,
        schema=final_schema,
        required_keys=["final_answer"],
        max_tokens=650,
        temperature=0.2,
        budgets=budgets,
    )
    budgets.record_cost(synthesized.provider, synthesized.estimated_cost, synthesized.tokens_in, synthesized.tokens_out)
    total_cost += synthesized.estimated_cost
    total_tokens_in += synthesized.tokens_in
    total_tokens_out += synthesized.tokens_out
    models.append(synthesized.model)

    if not synth_parsed.valid:
        warnings.append(f"Debate synthesis parse warning: {synth_parsed.error}")

    final_answer_text = _extract_final_answer_text(
        synthesized.text,
        synth_parsed.data if isinstance(synth_parsed.data, dict) else None,
    )
    if not final_answer_text or _word_count(final_answer_text) < 25:
        warnings.append("Debate synthesizer returned low/empty final answer; retrying rescue synthesizer.")
        logger.warning("SYNTH_EMPTY_RESCUE_TRIGGERED")
        rescue_prompt = (
            "The previous synthesis was empty or too short. Write a substantive final answer in plain prose. "
            "Start directly with the conclusion and include concrete tradeoffs.\n"
            f"<DEBATE_TOPIC>\n{query}\n</DEBATE_TOPIC>\n"
            f"<ARGUMENT_A>\n{_trim_for_prompt(arg_a, 1200)}\n</ARGUMENT_A>\n"
            f"<ARGUMENT_B>\n{_trim_for_prompt(arg_b, 1200)}\n</ARGUMENT_B>\n"
            f"<JUDGE>\nWinner={winner}; Reason={_trim_for_prompt(reason, 500)}\n</JUDGE>\n"
            f"<REQUIRED_FIXES>\n{_trim_for_prompt('; '.join(required_fixes), 500)}\n</REQUIRED_FIXES>"
        )
        rescued, rescued_parsed = await _call_structured_with_retry(
            adapter=synthesizer,
            model=synth_model,
            prompt=rescue_prompt,
            schema=final_schema,
            required_keys=["final_answer"],
            max_tokens=700,
            temperature=0.0,
            budgets=budgets,
        )
        budgets.record_cost(rescued.provider, rescued.estimated_cost, rescued.tokens_in, rescued.tokens_out)
        total_cost += rescued.estimated_cost
        total_tokens_in += rescued.tokens_in
        total_tokens_out += rescued.tokens_out
        models.append(rescued.model)
        if not rescued_parsed.valid:
            warnings.append(f"Debate rescue synthesis parse warning: {rescued_parsed.error}")
        final_answer_text = _extract_final_answer_text(
            rescued.text,
            rescued_parsed.data if isinstance(rescued_parsed.data, dict) else None,
        )
    if not final_answer_text:
        warnings.append("Debate synthesizer returned empty final answer; used deterministic fallback text.")
        logger.warning("SYNTH_EMPTY_FALLBACK_DETERMINISTIC")
        final_answer_text = _deterministic_debate_fallback(
            winner=winner,
            reason=reason,
            required_fixes=required_fixes,
            chosen_argument=chosen_argument,
        )
    final_answer = guardian.post_output(final_answer_text).redacted_text
    if not str(final_answer).strip():
        warnings.append("Debate final answer became empty after redaction; using deterministic fallback text.")
        fallback_text = _deterministic_debate_fallback(
            winner=winner,
            reason=reason,
            required_fixes=required_fixes,
            chosen_argument=chosen_argument,
        )
        final_answer = guardian.post_output(fallback_text).redacted_text.strip() or fallback_text

    return DebateWorkflowResult(
        final_answer=final_answer,
        argument_a=arg_a,
        argument_b=arg_b,
        judge_winner=winner,
        judge_reason=reason,
        required_fixes=required_fixes,
        total_cost=total_cost,
        total_tokens_in=total_tokens_in,
        total_tokens_out=total_tokens_out,
        models=models,
        warnings=warnings,
    )
