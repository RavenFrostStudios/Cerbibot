from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import re

from orchestrator.retrieval.fetch import RetrievedDocument
from orchestrator.retrieval.sanitize import sanitize_retrieved_text, wrap_untrusted_source
from orchestrator.security.taint import TaintedString


_EXCLUDED_DIRS = {".git", ".venv", ".pytest_cache", "__pycache__", "node_modules", ".next", "dist", "build"}
_CODE_SUFFIXES = {
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".json",
    ".yaml",
    ".yml",
    ".md",
    ".txt",
    ".toml",
    ".ini",
    ".cfg",
    ".sql",
    ".sh",
    ".go",
    ".rs",
    ".java",
    ".c",
    ".cpp",
    ".h",
}
_STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "that",
    "this",
    "from",
    "what",
    "when",
    "where",
    "which",
    "about",
    "into",
    "have",
    "need",
    "show",
    "find",
    "code",
}


@dataclass(slots=True)
class _Match:
    path: Path
    score: int
    line_no: int
    line_text: str


def search_workspace_code(
    query: str,
    *,
    workspace_root: Path | None = None,
    max_results: int = 5,
    max_files: int = 800,
    max_file_bytes: int = 200_000,
) -> list[RetrievedDocument]:
    docs, _meta = search_workspace_code_with_provenance(
        query,
        workspace_root=workspace_root,
        max_results=max_results,
        max_files=max_files,
        max_file_bytes=max_file_bytes,
    )
    return docs


def search_workspace_code_with_provenance(
    query: str,
    *,
    workspace_root: Path | None = None,
    max_results: int = 5,
    max_files: int = 800,
    max_file_bytes: int = 200_000,
) -> tuple[list[RetrievedDocument], dict[str, object]]:
    root = _resolve_workspace_root(workspace_root)
    tokens = _query_tokens(query)
    if not tokens:
        return [], {
            "workspace_root": str(root),
            "query_tokens": [],
            "scanned_files": 0,
            "matched_files": 0,
            "returned_documents": 0,
            "entries": [],
        }

    matches: list[_Match] = []
    scanned = 0
    for path in root.rglob("*"):
        if scanned >= max_files:
            break
        if not path.is_file() or _is_excluded(path):
            continue
        if path.suffix.lower() not in _CODE_SUFFIXES:
            continue
        try:
            if path.stat().st_size > max_file_bytes:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        scanned += 1
        match = _score_match(path, text, tokens)
        if match is not None:
            matches.append(match)

    matches.sort(key=lambda item: (item.score, -item.line_no), reverse=True)
    selected = matches[: max(1, max_results)]
    docs: list[RetrievedDocument] = []
    entries: list[dict[str, object]] = []
    now = datetime.now(timezone.utc).isoformat()
    for item in selected:
        rel = item.path.relative_to(root)
        snippet = sanitize_retrieved_text(f"{rel}:{item.line_no}\n{item.line_text}", max_chars=500)
        wrapped = wrap_untrusted_source(snippet)
        entries.append(
            {
                "path": str(rel),
                "line": item.line_no,
                "score": item.score,
            }
        )
        docs.append(
            RetrievedDocument(
                url=f"file://{item.path}#L{item.line_no}",
                title=str(rel),
                retrieved_at=now,
                text=TaintedString(
                    value=wrapped,
                    source="local_code_index",
                    source_id=f"{item.path}:{item.line_no}",
                    taint_level="untrusted",
                ),
            )
        )
    metadata: dict[str, object] = {
        "workspace_root": str(root),
        "query_tokens": tokens[:12],
        "scanned_files": scanned,
        "matched_files": len(matches),
        "returned_documents": len(docs),
        "entries": entries,
    }
    return docs, metadata


def _resolve_workspace_root(workspace_root: Path | None) -> Path:
    if workspace_root is not None:
        return workspace_root.resolve()
    env = os.getenv("MMO_WORKSPACE_ROOT", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return Path(os.getcwd()).resolve()


def _is_excluded(path: Path) -> bool:
    return any(part in _EXCLUDED_DIRS for part in path.parts)


def _query_tokens(query: str) -> list[str]:
    raw = re.findall(r"[a-zA-Z0-9_]{3,}", query.lower())
    return [token for token in raw if token not in _STOPWORDS]


def _score_match(path: Path, text: str, tokens: list[str]) -> _Match | None:
    lowered = text.lower()
    score = sum(lowered.count(token) for token in tokens)
    if score <= 0:
        return None
    best_line_no = 1
    best_line_text = ""
    best_line_score = -1
    for idx, line in enumerate(text.splitlines(), start=1):
        line_l = line.lower()
        line_score = sum(line_l.count(token) for token in tokens)
        if line_score > best_line_score:
            best_line_score = line_score
            best_line_no = idx
            best_line_text = line.strip()[:400]
    return _Match(path=path, score=score, line_no=best_line_no, line_text=best_line_text)
