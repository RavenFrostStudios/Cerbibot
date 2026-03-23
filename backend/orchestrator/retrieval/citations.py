from __future__ import annotations

from dataclasses import dataclass

from orchestrator.retrieval.fetch import RetrievedDocument


@dataclass(slots=True)
class Citation:
    url: str
    title: str
    retrieved_at: str
    snippet: str


def build_citations(documents: list[RetrievedDocument], snippet_chars: int = 220) -> list[Citation]:
    citations: list[Citation] = []
    for doc in documents:
        text = doc.text.value.replace("UNTRUSTED_SOURCE_BEGIN", "").replace("UNTRUSTED_SOURCE_END", "").strip()
        snippet = text[:snippet_chars]
        citations.append(
            Citation(
                url=doc.url,
                title=doc.title,
                retrieved_at=doc.retrieved_at,
                snippet=snippet,
            )
        )
    return citations


def format_citations_for_prompt(citations: list[Citation]) -> str:
    lines = ["Grounding sources:"]
    for idx, citation in enumerate(citations, start=1):
        lines.append(
            f"[{idx}] title={citation.title or 'Untitled'} url={citation.url} "
            f"retrieved_at={citation.retrieved_at} snippet={citation.snippet}"
        )
    return "\n".join(lines)
