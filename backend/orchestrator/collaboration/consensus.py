from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

from orchestrator.budgets import BudgetTracker
from orchestrator.collaboration.draft_critique_refine import _call_structured_with_retry
from orchestrator.providers.base import CompletionResult, ProviderAdapter
from orchestrator.retrieval.citations import Citation, build_citations, format_citations_for_prompt
from orchestrator.retrieval.fetch import fetch_url_content
from orchestrator.retrieval.search import search_web
from orchestrator.security.guardian import Guardian
from orchestrator.security.taint import TaintedString


@dataclass(slots=True)
class ConsensusProviderAnswer:
    provider: str
    model: str
    answer: str
    tokens_in: int
    tokens_out: int
    cost: float


@dataclass(slots=True)
class ConsensusWorkflowResult:
    final_answer: str
    answers_by_provider: dict[str, str]
    agreement_score: float
    confidence: float
    used_adjudication: bool
    adjudication_reason: str | None
    citations: list[Citation]
    total_cost: float
    total_tokens_in: int
    total_tokens_out: int
    models: list[str]
    warnings: list[str]


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _tokenize(text: str) -> set[str]:
    return {tok for tok in re.findall(r"[a-z0-9]{3,}", _normalize(text))}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a.intersection(b)) / len(a.union(b))


def _pairwise_agreement(answers: list[str]) -> float:
    if len(answers) <= 1:
        return 1.0
    scores: list[float] = []
    tokenized = [_tokenize(answer) for answer in answers]
    for idx in range(len(tokenized)):
        for jdx in range(idx + 1, len(tokenized)):
            scores.append(_jaccard(tokenized[idx], tokenized[jdx]))
    if not scores:
        return 1.0
    return sum(scores) / len(scores)


def _majority_answer(candidates: list[ConsensusProviderAnswer]) -> str:
    grouped: dict[str, list[ConsensusProviderAnswer]] = {}
    for candidate in candidates:
        grouped.setdefault(_normalize(candidate.answer), []).append(candidate)

    winning_group = max(grouped.values(), key=lambda items: (len(items), max(len(i.answer) for i in items)))
    return max(winning_group, key=lambda item: len(item.answer)).answer


def _coerce_confidence(value: object, default: float) -> float:
    if isinstance(value, (int, float)):
        return max(0.0, min(1.0, float(value)))
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"high", "strong"}:
            return 0.9
        if lowered in {"medium", "moderate"}:
            return 0.7
        if lowered in {"low", "weak"}:
            return 0.45
        try:
            parsed = float(lowered)
            return max(0.0, min(1.0, parsed))
        except ValueError:
            return default
    return default


async def _collect_answer(
    *,
    provider_name: str,
    adapter: ProviderAdapter,
    model: str,
    query: str,
    guardian: Guardian,
    budgets: BudgetTracker,
) -> ConsensusProviderAnswer:
    prompt = (
        "Answer the user's question independently. Keep it concise, factual, and do not reference other models.\n"
        f"Question: {query}"
    )
    estimated = adapter.estimate_cost(adapter.count_tokens(prompt, model), 450, model)
    budgets.check_would_fit(estimated)
    result: CompletionResult = await adapter.complete(prompt, model=model, max_tokens=450, temperature=0.1)
    clean = guardian.post_output(TaintedString(result.text, "model_output", f"{result.provider}:{result.model}").value).redacted_text
    return ConsensusProviderAnswer(
        provider=provider_name,
        model=result.model,
        answer=clean,
        tokens_in=result.tokens_in,
        tokens_out=result.tokens_out,
        cost=result.estimated_cost,
    )


async def run_consensus_workflow(
    *,
    query: str,
    participants: dict[str, tuple[ProviderAdapter, str]],
    adjudicator: ProviderAdapter,
    adjudicator_model: str,
    guardian: Guardian,
    budgets: BudgetTracker,
    retrieval_search_provider: str,
    retrieval_max_results: int,
    retrieval_timeout_seconds: float,
    retrieval_max_fetch_bytes: int,
    retrieval_domain_allowlist: list[str] | None,
    retrieval_domain_denylist: list[str] | None,
    agreement_threshold: float = 0.62,
) -> ConsensusWorkflowResult:
    if not participants:
        raise ValueError("Consensus mode requires at least one participant")

    warnings: list[str] = []
    models: list[str] = []
    total_cost = 0.0
    total_tokens_in = 0
    total_tokens_out = 0

    tasks = [
        _collect_answer(
            provider_name=provider_name,
            adapter=adapter,
            model=model,
            query=query,
            guardian=guardian,
            budgets=budgets,
        )
        for provider_name, (adapter, model) in participants.items()
    ]
    gathered = await asyncio.gather(*tasks, return_exceptions=True)
    candidates: list[ConsensusProviderAnswer] = []
    for item in gathered:
        if isinstance(item, Exception):
            warnings.append(f"Consensus provider failed: {item}")
            continue
        candidates.append(item)
        budgets.record_cost(item.provider, item.cost, item.tokens_in, item.tokens_out)
        total_cost += item.cost
        total_tokens_in += item.tokens_in
        total_tokens_out += item.tokens_out
        models.append(item.model)

    if not candidates:
        raise RuntimeError("Consensus mode failed: all providers errored")

    answers_by_provider = {candidate.provider: candidate.answer for candidate in candidates}
    agreement = _pairwise_agreement([candidate.answer for candidate in candidates])
    majority = _majority_answer(candidates)

    if len(candidates) == 1:
        warnings.append("Only one provider answered; confidence reduced.")
        return ConsensusWorkflowResult(
            final_answer=majority,
            answers_by_provider=answers_by_provider,
            agreement_score=agreement,
            confidence=0.45,
            used_adjudication=False,
            adjudication_reason=None,
            citations=[],
            total_cost=total_cost,
            total_tokens_in=total_tokens_in,
            total_tokens_out=total_tokens_out,
            models=models,
            warnings=warnings,
        )

    if agreement >= agreement_threshold:
        return ConsensusWorkflowResult(
            final_answer=majority,
            answers_by_provider=answers_by_provider,
            agreement_score=agreement,
            confidence=max(0.55, min(0.98, agreement)),
            used_adjudication=False,
            adjudication_reason=None,
            citations=[],
            total_cost=total_cost,
            total_tokens_in=total_tokens_in,
            total_tokens_out=total_tokens_out,
            models=models,
            warnings=warnings,
        )

    search_results = await search_web(
        query,
        provider=retrieval_search_provider,
        max_results=retrieval_max_results,
        timeout_seconds=retrieval_timeout_seconds,
        domain_allowlist=retrieval_domain_allowlist,
        domain_denylist=retrieval_domain_denylist,
    )
    fetch_tasks = [
        fetch_url_content(
            item.url,
            timeout_seconds=retrieval_timeout_seconds,
            max_bytes=retrieval_max_fetch_bytes,
        )
        for item in search_results
    ]
    fetched = await asyncio.gather(*fetch_tasks, return_exceptions=True)
    documents = [item for item in fetched if not isinstance(item, Exception)]
    for item in fetched:
        if isinstance(item, Exception):
            warnings.append(f"Consensus retrieval fetch failed: {item}")
    citations = build_citations(documents)
    grounding = format_citations_for_prompt(citations) if citations else "No retrieval sources available."

    answers_block = "\n\n".join(
        f"{candidate.provider} ({candidate.model}):\n{candidate.answer}" for candidate in candidates
    )
    adjudication_prompt = (
        "Resolve disagreement across candidate answers using provided sources.\n"
        "Return JSON with keys: final_answer (string), confidence (0..1 or low/medium/high), reason (string).\n"
        f"Question: {query}\n\nCandidate answers:\n{answers_block}\n\n{grounding}"
    )

    schema = {"final_answer": "string", "confidence": "number", "reason": "string"}
    adjudicated, parsed = await _call_structured_with_retry(
        adapter=adjudicator,
        model=adjudicator_model,
        prompt=adjudication_prompt,
        schema=schema,
        required_keys=["final_answer", "confidence", "reason"],
        max_tokens=650,
        temperature=0.1,
        budgets=budgets,
    )
    budgets.record_cost(
        adjudicated.provider,
        adjudicated.estimated_cost,
        adjudicated.tokens_in,
        adjudicated.tokens_out,
    )
    total_cost += adjudicated.estimated_cost
    total_tokens_in += adjudicated.tokens_in
    total_tokens_out += adjudicated.tokens_out
    models.append(adjudicated.model)

    if not parsed.valid:
        warnings.append(f"Consensus adjudication parse warning: {parsed.error}")

    parsed_data = parsed.data if isinstance(parsed.data, dict) else {}
    final_raw = str(parsed_data.get("final_answer", adjudicated.text))
    adjudication_reason = str(parsed_data.get("reason", "Adjudicated due to low agreement"))
    confidence = _coerce_confidence(parsed_data.get("confidence"), default=max(0.35, agreement))
    final_answer = guardian.post_output(final_raw).redacted_text

    return ConsensusWorkflowResult(
        final_answer=final_answer,
        answers_by_provider=answers_by_provider,
        agreement_score=agreement,
        confidence=confidence,
        used_adjudication=True,
        adjudication_reason=adjudication_reason,
        citations=citations,
        total_cost=total_cost,
        total_tokens_in=total_tokens_in,
        total_tokens_out=total_tokens_out,
        models=models,
        warnings=warnings,
    )
