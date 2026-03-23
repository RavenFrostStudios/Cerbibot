from __future__ import annotations

import re
from dataclasses import dataclass

from orchestrator.collaboration.output_parser import parse_structured_output
from orchestrator.providers.base import ProviderAdapter
from orchestrator.retrieval.fetch import fetch_url_content
from orchestrator.retrieval.search import search_web


@dataclass(slots=True)
class VerifiedClaim:
    claim: str
    classification: str
    verified: bool
    sources: list[str]
    conflicts: list[str]


TIME_SENSITIVE_HINTS = (
    "latest",
    "current",
    "today",
    "now",
    "released",
    "version",
    "price",
    "rate",
)


def classify_claim(claim: str) -> str:
    lowered = claim.lower()
    if any(hint in lowered for hint in TIME_SENSITIVE_HINTS):
        return "time_sensitive"
    if re.search(r"\b\d{4}\b", claim):
        return "slow_changing"
    return "timeless"


def fallback_extract_claims(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    claims = [p.strip() for p in parts if p.strip()]
    return claims[:10]


async def extract_claims(answer_text: str, adapter: ProviderAdapter, model: str) -> list[str]:
    schema = {"claims": "array"}
    prompt = (
        "Extract up to 10 atomic factual claims from the answer. "
        "Return JSON object with key claims as an array of strings.\n\n"
        f"Answer:\n{answer_text}"
    )
    result = await adapter.complete_structured(prompt, model, schema, max_tokens=400, temperature=0.0)
    parsed = parse_structured_output(result.text, ["claims"])
    if parsed.valid and isinstance(parsed.data.get("claims"), list):
        extracted = [str(item).strip() for item in parsed.data["claims"] if str(item).strip()]
        if extracted:
            return extracted[:10]
    return fallback_extract_claims(answer_text)


def _tokenize(text: str) -> set[str]:
    return {token for token in re.findall(r"[A-Za-z0-9_.-]+", text.lower()) if len(token) > 2}


async def verify_claim(claim: str, *, max_results: int, timeout_seconds: float, max_fetch_bytes: int) -> VerifiedClaim:
    classification = classify_claim(claim)
    if classification != "time_sensitive":
        return VerifiedClaim(claim=claim, classification=classification, verified=True, sources=[], conflicts=[])

    search_results = await search_web(claim, max_results=max_results, timeout_seconds=timeout_seconds)
    sources: list[str] = []
    conflicts: list[str] = []

    claim_tokens = _tokenize(claim)
    matched = False
    for item in search_results:
        try:
            doc = await fetch_url_content(item.url, timeout_seconds=timeout_seconds, max_bytes=max_fetch_bytes)
        except Exception as exc:
            conflicts.append(f"Fetch failed for {item.url}: {exc}")
            continue

        sources.append(doc.url)
        doc_tokens = _tokenize(doc.text.value)
        overlap = len(claim_tokens.intersection(doc_tokens))
        if overlap >= max(2, min(6, len(claim_tokens) // 3)):
            matched = True

    if not matched:
        conflicts.append("No strong supporting evidence found in retrieved sources")

    return VerifiedClaim(
        claim=claim,
        classification=classification,
        verified=matched,
        sources=sources,
        conflicts=conflicts,
    )


async def run_fact_check(
    *,
    answer_text: str,
    adapter: ProviderAdapter,
    model: str,
    max_results: int = 5,
    timeout_seconds: float = 10.0,
    max_fetch_bytes: int = 200_000,
) -> list[VerifiedClaim]:
    claims = await extract_claims(answer_text, adapter, model)
    results: list[VerifiedClaim] = []
    for claim in claims:
        results.append(
            await verify_claim(
                claim,
                max_results=max_results,
                timeout_seconds=timeout_seconds,
                max_fetch_bytes=max_fetch_bytes,
            )
        )
    return results
