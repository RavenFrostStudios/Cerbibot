from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from orchestrator.budgets import BudgetTracker
from orchestrator.collaboration.output_parser import parse_structured_output
from orchestrator.collaboration.quality_gates import (
    is_high_risk_query,
    is_low_signal_by_quality,
    is_low_signal_final_answer,
    is_meta_review_response,
    is_placeholder_response,
    is_policy_refusal_response,
    score_answer_quality,
)
from orchestrator.providers.base import ProviderAdapter
from orchestrator.security.guardian import Guardian
from orchestrator.security.taint import TaintedString


logger = logging.getLogger(__name__)

# Per-run circuit breaker for the refine stage: once we detect this many
# low-signal refiner outputs, skip additional rescue attempts.
_MAX_REFINER_LOW_SIGNAL_EVENTS_PER_RUN = 1


@dataclass(slots=True)
class CritiqueWorkflowResult:
    final_answer: str
    draft_text: str
    critique_text: str
    refine_text: str
    total_cost: float
    total_tokens_in: int
    total_tokens_out: int
    models: list[str]
    warnings: list[str]


def _has_useful_critique(payload: dict) -> bool:
    issues = payload.get("issues", [])
    missing = payload.get("missing", [])
    risk_flags = payload.get("risk_flags", [])
    return any(
        isinstance(items, list) and any(str(item).strip() for item in items)
        for items in (issues, missing, risk_flags)
    )


def _extract_or_fallback(text: str, key: str) -> str:
    try:
        data = json.loads(text)
        value = data.get(key)
        if isinstance(value, str):
            return value
    except json.JSONDecodeError:
        pass
    return text


def _coerce_structured_payload(schema: dict[str, str], payload: dict | None, *, stage: str) -> dict:
    data = payload if isinstance(payload, dict) else {}
    coerced: dict[str, object] = {}
    for key, schema_type in schema.items():
        value = data.get(key)
        if schema_type == "string":
            coerced[key] = str(value).strip() if value is not None else ""
        elif schema_type == "array":
            if isinstance(value, list):
                coerced[key] = [str(item) for item in value]
            elif isinstance(value, str) and value.strip():
                coerced[key] = [value.strip()]
            else:
                coerced[key] = []
        else:
            coerced[key] = value if value is not None else ""
    coerced["_stage"] = stage
    return coerced


def _format_critique_payload(payload: dict) -> str:
    issues = payload.get("issues", [])
    missing = payload.get("missing", [])
    risk_flags = payload.get("risk_flags", [])

    def _fmt(items: object) -> str:
        if not isinstance(items, list) or not items:
            return "none"
        return "; ".join(str(item) for item in items)

    return (
        f"Issues: {_fmt(issues)}\n"
        f"Missing: {_fmt(missing)}\n"
        f"Risk Flags: {_fmt(risk_flags)}"
    )


def _fallback_final_from_draft_and_critique(draft_text: str, critique_text: str) -> str:
    draft_clean = str(draft_text or "").strip()
    critique_clean = str(critique_text or "").strip()
    draft_unusable = (
        not draft_clean
        or is_placeholder_response(draft_clean)
        or is_policy_refusal_response(draft_clean)
        or is_meta_review_response(draft_clean)
    )
    if draft_unusable and critique_clean:
        return f"[Critique Notes]\n{critique_clean}"
    if draft_unusable:
        return "No response content was generated. Please retry."
    if not critique_clean:
        return draft_clean
    return f"{draft_clean}\n\n[Critique Notes]\n{critique_clean}"


def _deterministic_refinement_from_draft_and_critique(draft_text: str, critique_text: str) -> str:
    draft_clean = str(draft_text or "").strip()
    critique_clean = str(critique_text or "").strip()
    if is_low_signal_final_answer(draft_clean):
        return _fallback_final_from_draft_and_critique(draft_text, critique_text)

    improvements: list[str] = []
    for raw_line in critique_clean.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.lower().startswith(("issues:", "missing:", "risk flags:")):
            content = line.split(":", 1)[1].strip()
            if content and content.lower() != "none":
                improvements.append(content)
        elif line.lower() != "none":
            improvements.append(line)

    if not improvements:
        return draft_clean

    bullet_lines = "\n".join(f"- {item}" for item in improvements[:6])
    return (
        f"{draft_clean}\n\n"
        "Refinement Notes Applied:\n"
        f"{bullet_lines}"
    )


def _looks_jsonish(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    if stripped.startswith("{") or stripped.startswith("["):
        return True
    if "```json" in stripped.lower():
        return True
    return "{" in stripped and "}" in stripped


def _should_warn_parse_failure(text: str) -> bool:
    # If the model produced JSON-like output, we can usually salvage structured fields.
    return not _looks_jsonish(text)


def _parsed_payload_or_empty(parsed_data: dict, raw_text: str) -> dict:
    if _looks_jsonish(raw_text):
        return parsed_data
    return {}


async def _call_structured_with_retry(
    *,
    adapter: ProviderAdapter,
    model: str,
    prompt: str,
    schema: dict,
    required_keys: list[str],
    max_tokens: int,
    temperature: float,
    budgets: BudgetTracker,
) -> tuple:
    def _schema_defaults(schema_map: dict) -> dict[str, object]:
        defaults: dict[str, object] = {}
        for key, kind in schema_map.items():
            if kind == "array":
                defaults[key] = []
            elif kind == "string":
                defaults[key] = ""
            else:
                defaults[key] = ""
        return defaults

    estimate = adapter.estimate_cost(adapter.count_tokens(prompt, model), max_tokens, model)
    budgets.check_would_fit(estimate)
    result = await adapter.complete_structured(
        prompt,
        model,
        schema,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    parsed = parse_structured_output(result.text, required_keys)
    if parsed.valid:
        return result, parsed

    repair_prompt = (
        "Return ONLY valid JSON object with required keys "
        f"{required_keys}. No prose.\nOriginal response:\n{result.text}"
    )
    repair_estimate = adapter.estimate_cost(adapter.count_tokens(repair_prompt, model), max_tokens, model)
    budgets.check_would_fit(repair_estimate)
    repaired = await adapter.complete_structured(
        repair_prompt,
        model,
        schema,
        max_tokens=max_tokens,
        temperature=0.0,
    )
    reparsed = parse_structured_output(repaired.text, required_keys)
    if reparsed.valid:
        return repaired, reparsed

    defaults = _schema_defaults(schema)
    repair_prompt_compact = (
        "Return ONLY strict valid JSON. No markdown, no prose, no code fences.\n"
        f"Required keys: {required_keys}\n"
        f"JSON template: {json.dumps(defaults, ensure_ascii=True)}\n"
        "Keep values concise so output stays under token limit."
    )
    compact_estimate = adapter.estimate_cost(adapter.count_tokens(repair_prompt_compact, model), max_tokens, model)
    budgets.check_would_fit(compact_estimate)
    compact = await adapter.complete_structured(
        repair_prompt_compact,
        model,
        schema,
        max_tokens=max_tokens,
        temperature=0.0,
    )
    compact_parsed = parse_structured_output(compact.text, required_keys)
    if compact_parsed.valid:
        return compact, compact_parsed
    return compact, compact_parsed


async def run_workflow(
    prompt: str,
    drafter: ProviderAdapter,
    critic: ProviderAdapter,
    refiner: ProviderAdapter,
    drafter_model: str,
    critic_model: str,
    refiner_model: str,
    guardian: Guardian,
    budgets: BudgetTracker,
) -> CritiqueWorkflowResult:
    draft_schema = {"answer": "string", "assumptions": "array", "needs_verification": "array"}
    critique_schema = {"issues": "array", "missing": "array", "risk_flags": "array"}
    refine_schema = {"final_answer": "string", "citations": "array", "confidence": "string"}

    draft_prompt = f"You are a precise drafter. Question: {prompt}"
    warnings: list[str] = []
    total_cost = 0.0
    total_tokens_in = 0
    total_tokens_out = 0
    low_signal_refiner_events = 0

    draft, draft_parsed = await _call_structured_with_retry(
        adapter=drafter,
        model=drafter_model,
        prompt=draft_prompt,
        schema=draft_schema,
        required_keys=["answer", "assumptions", "needs_verification"],
        max_tokens=700,
        temperature=0.2,
        budgets=budgets,
    )
    budgets.record_cost(draft.provider, draft.estimated_cost, draft.tokens_in, draft.tokens_out)
    total_cost += draft.estimated_cost
    total_tokens_in += draft.tokens_in
    total_tokens_out += draft.tokens_out

    if not draft_parsed.valid and _should_warn_parse_failure(draft.text):
        warning = f"Drafter structured parse failed after retry: {draft_parsed.error}"
        warnings.append(warning)
        logger.warning("workflow_warning", extra={"stage": "draft", "warning": warning})
    elif not draft_parsed.valid:
        logger.info("workflow_parse_recovered", extra={"stage": "draft", "error": draft_parsed.error})

    draft_payload = _coerce_structured_payload(
        draft_schema,
        _parsed_payload_or_empty(draft_parsed.data, draft.text),
        stage="draft",
    )
    draft_text_raw = str(draft_payload.get("answer", "")).strip() or "No response content was generated. Please retry."
    if is_placeholder_response(draft_text_raw) or is_meta_review_response(draft_text_raw):
        strict_draft_prompt = (
            "Return ONLY valid JSON with keys answer, assumptions, needs_verification. "
            "answer must directly answer the question, must be non-empty, and must not contain "
            "'ready for query', 'no substantive query provided', or similar placeholders. "
            "Do not critique an 'original response'; directly complete the requested task. "
            f"Question: {prompt}"
        )
        draft_strict, draft_strict_parsed = await _call_structured_with_retry(
            adapter=drafter,
            model=drafter_model,
            prompt=strict_draft_prompt,
            schema=draft_schema,
            required_keys=["answer", "assumptions", "needs_verification"],
            max_tokens=500,
            temperature=0.0,
            budgets=budgets,
        )
        budgets.record_cost(
            draft_strict.provider,
            draft_strict.estimated_cost,
            draft_strict.tokens_in,
            draft_strict.tokens_out,
        )
        total_cost += draft_strict.estimated_cost
        total_tokens_in += draft_strict.tokens_in
        total_tokens_out += draft_strict.tokens_out
        if not draft_strict_parsed.valid and _should_warn_parse_failure(draft_strict.text):
            warning = f"Drafter strict retry parse warning: {draft_strict_parsed.error}"
            warnings.append(warning)
            logger.warning("workflow_warning", extra={"stage": "draft_strict_retry", "warning": warning})
        draft_payload = _coerce_structured_payload(
            draft_schema,
            _parsed_payload_or_empty(draft_strict_parsed.data, draft_strict.text),
            stage="draft",
        )
        draft_text_raw = str(draft_payload.get("answer", "")).strip() or draft_text_raw
    draft_tainted = TaintedString(
        value=draft_text_raw,
        source="model_output",
        source_id=f"{draft.provider}:{draft.model}",
        taint_level="untrusted",
    )
    draft_checked = guardian.post_output(draft_tainted.value)
    draft_text = draft_checked.redacted_text

    critique_prompt = f"Critique this draft for correctness and omissions. Draft: {draft_text}"
    critique_text = ""
    critique = None
    try:
        critique, critique_parsed = await _call_structured_with_retry(
            adapter=critic,
            model=critic_model,
            prompt=critique_prompt,
            schema=critique_schema,
            required_keys=["issues", "missing", "risk_flags"],
            max_tokens=600,
            temperature=0.1,
            budgets=budgets,
        )
        if not critique_parsed.valid and _should_warn_parse_failure(critique.text):
            warning = f"Critic structured parse failed after retry: {critique_parsed.error}"
            warnings.append(warning)
            logger.warning("workflow_warning", extra={"stage": "critique", "warning": warning})
        elif not critique_parsed.valid:
            logger.info("workflow_parse_recovered", extra={"stage": "critique", "error": critique_parsed.error})
        budgets.record_cost(critique.provider, critique.estimated_cost, critique.tokens_in, critique.tokens_out)
        total_cost += critique.estimated_cost
        total_tokens_in += critique.tokens_in
        total_tokens_out += critique.tokens_out

        critique_payload = _coerce_structured_payload(
            critique_schema,
            _parsed_payload_or_empty(critique_parsed.data, critique.text),
            stage="critique",
        )
        if not _has_useful_critique(critique_payload):
            strict_critique_prompt = (
                "Return ONLY valid JSON with keys issues, missing, risk_flags. "
                "Provide at least one concrete item across those arrays unless the draft is perfect; "
                "if perfect, include one risk_flags item explaining residual risk. "
                f"Question: {prompt}\nDraft: {draft_text}"
            )
            critique_strict, critique_strict_parsed = await _call_structured_with_retry(
                adapter=critic,
                model=critic_model,
                prompt=strict_critique_prompt,
                schema=critique_schema,
                required_keys=["issues", "missing", "risk_flags"],
                max_tokens=360,
                temperature=0.0,
                budgets=budgets,
            )
            budgets.record_cost(
                critique_strict.provider,
                critique_strict.estimated_cost,
                critique_strict.tokens_in,
                critique_strict.tokens_out,
            )
            total_cost += critique_strict.estimated_cost
            total_tokens_in += critique_strict.tokens_in
            total_tokens_out += critique_strict.tokens_out
            if not critique_strict_parsed.valid and _should_warn_parse_failure(critique_strict.text):
                warning = f"Critic strict retry parse warning: {critique_strict_parsed.error}"
                warnings.append(warning)
                logger.warning("workflow_warning", extra={"stage": "critique_strict_retry", "warning": warning})
            critique_payload = _coerce_structured_payload(
                critique_schema,
                _parsed_payload_or_empty(critique_strict_parsed.data, critique_strict.text),
                stage="critique",
            )
        critique_text_raw = _format_critique_payload(critique_payload)
        critique_tainted = TaintedString(
            value=critique_text_raw,
            source="model_output",
            source_id=f"{critique.provider}:{critique.model}",
            taint_level="untrusted",
        )
        critique_checked = guardian.post_output(critique_tainted.value)
        critique_text = critique_checked.redacted_text
    except Exception as exc:
        warning = f"Critique step failed; returned draft-only answer. reason={exc}"
        warnings.append(warning)
        logger.warning("workflow_fallback", extra={"stage": "critique", "warning": warning})
        return CritiqueWorkflowResult(
            final_answer=draft_text,
            draft_text=draft_text,
            critique_text="",
            refine_text="",
            total_cost=total_cost,
            total_tokens_in=total_tokens_in,
            total_tokens_out=total_tokens_out,
            models=[draft.model],
            warnings=warnings,
        )

    refine_prompt = (
        "Refine the final answer using the draft and critique. "
        f"Question: {prompt}\nDraft: {draft_text}\nCritique: {critique_text}"
    )
    try:
        refined, refined_parsed = await _call_structured_with_retry(
            adapter=refiner,
            model=refiner_model,
            prompt=refine_prompt,
            schema=refine_schema,
            required_keys=["final_answer", "citations", "confidence"],
            max_tokens=700,
            temperature=0.2,
            budgets=budgets,
        )
        if not refined_parsed.valid and _should_warn_parse_failure(refined.text):
            warning = f"Refiner structured parse failed after retry: {refined_parsed.error}"
            warnings.append(warning)
            logger.warning("workflow_warning", extra={"stage": "refine", "warning": warning})
        elif not refined_parsed.valid:
            logger.info("workflow_parse_recovered", extra={"stage": "refine", "error": refined_parsed.error})
        budgets.record_cost(refined.provider, refined.estimated_cost, refined.tokens_in, refined.tokens_out)
        total_cost += refined.estimated_cost
        total_tokens_in += refined.tokens_in
        total_tokens_out += refined.tokens_out

        refined_payload = _coerce_structured_payload(
            refine_schema,
            _parsed_payload_or_empty(refined_parsed.data, refined.text),
            stage="refine",
        )
        final_answer_candidate = str(refined_payload.get("final_answer", "")).strip()
        refiner_circuit_open = False
        candidate_low_signal = (
            not final_answer_candidate
            or is_placeholder_response(final_answer_candidate)
            or is_low_signal_by_quality(final_answer_candidate, user_query=prompt)
        )
        if candidate_low_signal:
            low_signal_refiner_events += 1
            refiner_circuit_open = low_signal_refiner_events >= _MAX_REFINER_LOW_SIGNAL_EVENTS_PER_RUN
        if candidate_low_signal and not refiner_circuit_open:
            strict_refine_prompt = (
                "Return ONLY valid JSON with keys final_answer, citations, confidence. "
                "final_answer must be non-empty, concise, and directly answer the question; "
                "do not use placeholder text like 'ready for query' or 'no substantive query provided'. "
                f"Question: {prompt}\nDraft: {draft_text}\nCritique: {critique_text}"
            )
            refined_strict, refined_strict_parsed = await _call_structured_with_retry(
                adapter=refiner,
                model=refiner_model,
                prompt=strict_refine_prompt,
                schema=refine_schema,
                required_keys=["final_answer", "citations", "confidence"],
                max_tokens=360,
                temperature=0.0,
                budgets=budgets,
            )
            budgets.record_cost(
                refined_strict.provider,
                refined_strict.estimated_cost,
                refined_strict.tokens_in,
                refined_strict.tokens_out,
            )
            total_cost += refined_strict.estimated_cost
            total_tokens_in += refined_strict.tokens_in
            total_tokens_out += refined_strict.tokens_out
            if not refined_strict_parsed.valid and _should_warn_parse_failure(refined_strict.text):
                warning = f"Refiner strict retry parse warning: {refined_strict_parsed.error}"
                warnings.append(warning)
                logger.warning("workflow_warning", extra={"stage": "refine_strict_retry", "warning": warning})
            refined_payload = _coerce_structured_payload(
                refine_schema,
                _parsed_payload_or_empty(refined_strict_parsed.data, refined_strict.text),
                stage="refine",
            )
            final_answer_candidate = str(refined_payload.get("final_answer", "")).strip()
            candidate_low_signal = (
                not final_answer_candidate
                or is_placeholder_response(final_answer_candidate)
                or is_low_signal_by_quality(final_answer_candidate, user_query=prompt)
            )
            if candidate_low_signal:
                low_signal_refiner_events += 1
                refiner_circuit_open = low_signal_refiner_events >= _MAX_REFINER_LOW_SIGNAL_EVENTS_PER_RUN
        final_candidate = str(refined_payload.get("final_answer", "")).strip()
        refusal_for_benign = is_policy_refusal_response(final_candidate) and not is_high_risk_query(prompt)
        meta_for_benign = is_meta_review_response(final_candidate) and not is_high_risk_query(prompt)
        quality_low = is_low_signal_by_quality(final_candidate, user_query=prompt)
        if not final_candidate or is_placeholder_response(final_candidate) or refusal_for_benign or meta_for_benign or quality_low:
            warning = "Refiner produced empty/placeholder final answer; used draft+critique fallback."
            if refusal_for_benign:
                warning = "Refiner produced policy-refusal on benign prompt; used draft+critique fallback."
            elif meta_for_benign:
                warning = "Refiner produced meta-review on benign prompt; used draft+critique fallback."
            elif quality_low and final_candidate:
                score, reasons = score_answer_quality(final_candidate, user_query=prompt)
                warning = (
                    "Refiner produced low-quality final answer "
                    f"(score={score:.2f}, reasons={','.join(reasons) or 'unknown'}); "
                    "used draft+critique fallback."
                )
            warnings.append(warning)
            logger.warning("workflow_warning", extra={"stage": "refine_fallback", "warning": warning})
            refined_payload["final_answer"] = _fallback_final_from_draft_and_critique(draft_text, critique_text)
            # If fallback is still weak, do one direct rescue attempt with question-only context.
            fallback_candidate = str(refined_payload.get("final_answer", "")).strip()
            if is_low_signal_final_answer(fallback_candidate):
                if refiner_circuit_open:
                    deterministic = _deterministic_refinement_from_draft_and_critique(draft_text, critique_text)
                    refined_payload["final_answer"] = deterministic
                    cb_warning = (
                        "Refiner circuit breaker engaged after low-signal output; "
                        "skipped additional rescue attempts and used deterministic local refinement."
                    )
                    warnings.append(cb_warning)
                    logger.warning("workflow_warning", extra={"stage": "refine_circuit_breaker", "warning": cb_warning})
                    refined_text_raw = json.dumps(refined_payload, ensure_ascii=True, sort_keys=True)
                    refined_tainted = TaintedString(
                        value=refined_text_raw,
                        source="model_output",
                        source_id=f"{refined.provider}:{refined.model}",
                        taint_level="untrusted",
                    )
                    refined_checked = guardian.post_output(refined_tainted.value)
                    final_answer = _extract_or_fallback(refined_checked.redacted_text, "final_answer")
                    refine_text = refined_checked.redacted_text
                    models = [draft.model, critique.model if critique else critic_model, refined.model]
                    return CritiqueWorkflowResult(
                        final_answer=final_answer,
                        draft_text=draft_text,
                        critique_text=critique_text,
                        refine_text=refine_text,
                        total_cost=total_cost,
                        total_tokens_in=total_tokens_in,
                        total_tokens_out=total_tokens_out,
                        models=models,
                        warnings=warnings,
                    )
                rewritten_query = prompt
                if not is_high_risk_query(prompt):
                    rewrite_schema = {"rewritten_query": "string"}
                    rewrite_prompt = (
                        "Return ONLY valid JSON with key rewritten_query. "
                        "Rewrite the user's request to be clearer and directly actionable, without changing intent."
                        f"\nUser request: {prompt}"
                    )
                    rewrite_result, rewrite_parsed = await _call_structured_with_retry(
                        adapter=refiner,
                        model=refiner_model,
                        prompt=rewrite_prompt,
                        schema=rewrite_schema,
                        required_keys=["rewritten_query"],
                        max_tokens=180,
                        temperature=0.0,
                        budgets=budgets,
                    )
                    budgets.record_cost(
                        rewrite_result.provider,
                        rewrite_result.estimated_cost,
                        rewrite_result.tokens_in,
                        rewrite_result.tokens_out,
                    )
                    total_cost += rewrite_result.estimated_cost
                    total_tokens_in += rewrite_result.tokens_in
                    total_tokens_out += rewrite_result.tokens_out
                    rewrite_payload = _coerce_structured_payload(
                        rewrite_schema,
                        _parsed_payload_or_empty(rewrite_parsed.data, rewrite_result.text),
                        stage="rewrite",
                    )
                    rewrite_candidate = str(rewrite_payload.get("rewritten_query", "")).strip()
                    rewrite_bad = (
                        not rewrite_candidate
                        or is_placeholder_response(rewrite_candidate)
                        or is_policy_refusal_response(rewrite_candidate)
                        or is_meta_review_response(rewrite_candidate)
                    )
                    if not rewrite_bad:
                        rewritten_query = rewrite_candidate

                rescue_prompt = (
                    "Return ONLY valid JSON with keys final_answer, citations, confidence. "
                    "final_answer must directly answer the user's question, be non-empty, and avoid "
                    "placeholder/refusal/meta-review text.\n"
                    f"Question: {rewritten_query}"
                )
                rescue, rescue_parsed = await _call_structured_with_retry(
                    adapter=refiner,
                    model=refiner_model,
                    prompt=rescue_prompt,
                    schema=refine_schema,
                    required_keys=["final_answer", "citations", "confidence"],
                    max_tokens=420,
                    temperature=0.0,
                    budgets=budgets,
                )
                budgets.record_cost(
                    rescue.provider,
                    rescue.estimated_cost,
                    rescue.tokens_in,
                    rescue.tokens_out,
                )
                total_cost += rescue.estimated_cost
                total_tokens_in += rescue.tokens_in
                total_tokens_out += rescue.tokens_out
                rescue_payload = _coerce_structured_payload(
                    refine_schema,
                    _parsed_payload_or_empty(rescue_parsed.data, rescue.text),
                    stage="refine",
                )
                rescue_answer = str(rescue_payload.get("final_answer", "")).strip()
                rescue_bad = is_low_signal_final_answer(rescue_answer)
                if not rescue_bad:
                    refined_payload = rescue_payload
                    ok_warning = "Refiner fallback rescue succeeded with direct question-only retry."
                    warnings.append(ok_warning)
                    logger.info("workflow_recovered", extra={"stage": "refine_fallback_rescue", "warning": ok_warning})
                else:
                    deterministic = _deterministic_refinement_from_draft_and_critique(draft_text, critique_text)
                    refined_payload["final_answer"] = deterministic
                    det_warning = "Refiner fallback rescue remained low-signal; used deterministic local refinement."
                    warnings.append(det_warning)
                    logger.warning("workflow_warning", extra={"stage": "refine_deterministic_fallback", "warning": det_warning})
        refined_text_raw = json.dumps(refined_payload, ensure_ascii=True, sort_keys=True)
        refined_tainted = TaintedString(
            value=refined_text_raw,
            source="model_output",
            source_id=f"{refined.provider}:{refined.model}",
            taint_level="untrusted",
        )
        refined_checked = guardian.post_output(refined_tainted.value)
        final_answer = _extract_or_fallback(refined_checked.redacted_text, "final_answer")
        refine_text = refined_checked.redacted_text
        models = [draft.model, critique.model if critique else critic_model, refined.model]
    except Exception as exc:
        warning = f"Refine step failed; returned draft with critique context. reason={exc}"
        warnings.append(warning)
        logger.warning("workflow_fallback", extra={"stage": "refine", "warning": warning})
        final_answer = f"{draft_text}\n\n[Critique Notes]\n{critique_text}"
        refine_text = ""
        models = [draft.model, critique.model if critique else critic_model]

    return CritiqueWorkflowResult(
        final_answer=final_answer,
        draft_text=draft_text,
        critique_text=critique_text,
        refine_text=refine_text,
        total_cost=total_cost,
        total_tokens_in=total_tokens_in,
        total_tokens_out=total_tokens_out,
        models=models,
        warnings=warnings,
    )
