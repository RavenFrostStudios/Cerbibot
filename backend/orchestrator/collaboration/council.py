from __future__ import annotations

from dataclasses import dataclass

from orchestrator.budgets import BudgetTracker
from orchestrator.collaboration.draft_critique_refine import _call_structured_with_retry
from orchestrator.providers.base import ProviderAdapter
from orchestrator.security.guardian import Guardian


@dataclass(slots=True)
class SpecialistOutput:
    role: str
    provider: str
    model: str
    text: str


@dataclass(slots=True)
class CouncilWorkflowResult:
    final_answer: str
    specialists: list[SpecialistOutput]
    synthesis_notes: str
    total_cost: float
    total_tokens_in: int
    total_tokens_out: int
    models: list[str]
    warnings: list[str]


async def run_council_workflow(
    *,
    query: str,
    specialists: list[tuple[str, ProviderAdapter, str]],
    synthesizer: ProviderAdapter,
    synthesizer_model: str,
    guardian: Guardian,
    budgets: BudgetTracker,
) -> CouncilWorkflowResult:
    warnings: list[str] = []
    total_cost = 0.0
    total_tokens_in = 0
    total_tokens_out = 0
    models: list[str] = []
    outputs: list[SpecialistOutput] = []

    specialist_schema = {
        "answer": "string",
        "claims": "array",
        "assumptions": "array",
        "evidence_needed": "array",
    }
    synth_schema = {"final_answer": "string", "notes": "string"}

    for role, adapter, model in specialists:
        prompt = (
            f"You are the {role} specialist in a council. "
            "Provide your best structured analysis for the user's query.\n"
            f"Query: {query}"
        )
        result, parsed = await _call_structured_with_retry(
            adapter=adapter,
            model=model,
            prompt=prompt,
            schema=specialist_schema,
            required_keys=["answer", "claims", "assumptions", "evidence_needed"],
            max_tokens=600,
            temperature=0.2,
            budgets=budgets,
        )
        budgets.record_cost(result.provider, result.estimated_cost, result.tokens_in, result.tokens_out)
        total_cost += result.estimated_cost
        total_tokens_in += result.tokens_in
        total_tokens_out += result.tokens_out
        models.append(result.model)
        if not parsed.valid:
            warnings.append(f"{role} parse warning: {parsed.error}")
        cleaned = guardian.post_output(result.text).redacted_text
        outputs.append(SpecialistOutput(role=role, provider=result.provider, model=result.model, text=cleaned))

    specialist_block = "\n\n".join(
        f"[{item.role}] provider={item.provider} model={item.model}\n{item.text}" for item in outputs
    )
    synth_prompt = (
        "Synthesize a single final answer from specialist council inputs. "
        "Resolve disagreements and include necessary caveats.\n"
        f"User query: {query}\n\nSpecialist inputs:\n{specialist_block}"
    )
    synthesized, synth_parsed = await _call_structured_with_retry(
        adapter=synthesizer,
        model=synthesizer_model,
        prompt=synth_prompt,
        schema=synth_schema,
        required_keys=["final_answer", "notes"],
        max_tokens=700,
        temperature=0.2,
        budgets=budgets,
    )
    budgets.record_cost(synthesized.provider, synthesized.estimated_cost, synthesized.tokens_in, synthesized.tokens_out)
    total_cost += synthesized.estimated_cost
    total_tokens_in += synthesized.tokens_in
    total_tokens_out += synthesized.tokens_out
    models.append(synthesized.model)
    if not synth_parsed.valid:
        warnings.append(f"Council synthesis parse warning: {synth_parsed.error}")

    parsed_data = synth_parsed.data if isinstance(synth_parsed.data, dict) else {}
    final_answer = guardian.post_output(str(parsed_data.get("final_answer", synthesized.text))).redacted_text
    notes = guardian.post_output(str(parsed_data.get("notes", ""))).redacted_text

    return CouncilWorkflowResult(
        final_answer=final_answer,
        specialists=outputs,
        synthesis_notes=notes,
        total_cost=total_cost,
        total_tokens_in=total_tokens_in,
        total_tokens_out=total_tokens_out,
        models=models,
        warnings=warnings,
    )
