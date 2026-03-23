from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from orchestrator.retrieval.sanitize import html_to_text, sanitize_retrieved_text, wrap_untrusted_source
from orchestrator.security.scanners import is_ssrf_risky_url

_SEARCH_EXCLUDED_DIRS = {".git", ".venv", ".pytest_cache", "__pycache__", "node_modules"}


def resolve_workspace_root() -> Path:
    return Path(os.getenv("MMO_WORKSPACE_ROOT", os.getcwd())).resolve()


def resolve_workspace_path(raw_path: str) -> Path:
    if not raw_path.strip():
        raise ValueError("path cannot be empty")
    root = resolve_workspace_root()
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"path escapes workspace root: {raw_path}")
    return resolved


def run_file_read(args: dict[str, str]) -> dict[str, object]:
    path = resolve_workspace_path(args.get("path", ""))
    if not path.exists():
        raise ValueError(f"path not found: {path}")
    if not path.is_file():
        raise ValueError(f"path is not a file: {path}")
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) > 50_000:
        text = text[:50_000]
    return {
        "status": "ok",
        "tool": "file_read",
        "path": str(path),
        "stdout": text,
        "stderr": "",
        "exit_code": 0,
    }


def run_file_search(args: dict[str, str]) -> dict[str, object]:
    root = resolve_workspace_path(args.get("path", "."))
    if root.is_file():
        roots = [root]
    elif root.is_dir():
        roots = [p for p in root.rglob("*") if p.is_file() and not _is_excluded(p)]
    else:
        raise ValueError(f"path not found: {root}")
    needle = args.get("pattern", "")
    if not needle.strip():
        raise ValueError("pattern cannot be empty")

    matches: list[dict[str, object]] = []
    for file_path in roots[:300]:
        try:
            text = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            if needle in line:
                matches.append(
                    {
                        "path": str(file_path),
                        "line": line_no,
                        "text": line[:500],
                    }
                )
                if len(matches) >= 200:
                    break
        if len(matches) >= 200:
            break
    return {
        "status": "ok",
        "tool": "file_search",
        "stdout": json.dumps({"count": len(matches), "matches": matches}, ensure_ascii=True),
        "stderr": "",
        "exit_code": 0,
    }


def run_git_status(args: dict[str, str]) -> dict[str, object]:
    path = resolve_workspace_path(args.get("path", "."))
    if path.is_file():
        path = path.parent
    command = ["git", "-C", str(path), "status", "--short", "--branch"]
    proc = subprocess.run(command, capture_output=True, text=True, timeout=8, check=False)
    return {
        "status": "ok",
        "tool": "git_status",
        "stdout": proc.stdout[:20_000],
        "stderr": proc.stderr[:5_000],
        "exit_code": proc.returncode,
    }


def run_web_retrieve(args: dict[str, str]) -> dict[str, object]:
    url = args.get("url", "").strip()
    if not url:
        raise ValueError("url cannot be empty")
    if is_ssrf_risky_url(url):
        raise ValueError(f"blocked risky URL: {url}")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("only http/https URLs are supported")

    req = Request(url, headers={"User-Agent": "MMO-Tool/1.0"})
    with urlopen(req, timeout=10) as resp:
        body = resp.read(200_000).decode("utf-8", errors="replace")
    clean = sanitize_retrieved_text(html_to_text(body), max_chars=50_000)
    wrapped = wrap_untrusted_source(clean)
    return {
        "status": "ok",
        "tool": "web_retrieve",
        "url": url,
        "stdout": wrapped,
        "stderr": "",
        "exit_code": 0,
    }


def run_json_query(args: dict[str, str]) -> dict[str, object]:
    raw = args.get("json_text", "")
    query = args.get("query", "")
    if not raw.strip():
        raise ValueError("json_text cannot be empty")
    if not query.strip():
        raise ValueError("query cannot be empty")
    data = json.loads(raw)
    value = _query_json_path(data, query)
    return {
        "status": "ok",
        "tool": "json_query",
        "stdout": json.dumps({"query": query, "value": value}, ensure_ascii=True),
        "stderr": "",
        "exit_code": 0,
    }


def run_regex_test(args: dict[str, str]) -> dict[str, object]:
    pattern = args.get("pattern", "")
    text = args.get("text", "")
    if not pattern.strip():
        raise ValueError("pattern cannot be empty")
    compiled = re.compile(pattern)
    matches: list[dict[str, object]] = []
    for item in compiled.finditer(text):
        matches.append({"match": item.group(0), "start": item.start(), "end": item.end()})
        if len(matches) >= 100:
            break
    return {
        "status": "ok",
        "tool": "regex_test",
        "stdout": json.dumps({"count": len(matches), "matches": matches}, ensure_ascii=True),
        "stderr": "",
        "exit_code": 0,
    }


def run_system_info(_args: dict[str, str]) -> dict[str, object]:
    root = resolve_workspace_root()
    disk = shutil.disk_usage(root)
    payload = {
        "platform": platform.platform(),
        "python_version": sys.version.split(" ", 1)[0],
        "workspace_root": str(root),
        "cpu_count": os.cpu_count(),
        "disk_total_bytes": disk.total,
        "disk_free_bytes": disk.free,
    }
    return {
        "status": "ok",
        "tool": "system_info",
        "stdout": json.dumps(payload, ensure_ascii=True),
        "stderr": "",
        "exit_code": 0,
    }


def _is_excluded(path: Path) -> bool:
    return any(part in _SEARCH_EXCLUDED_DIRS for part in path.parts)


def _query_json_path(data: object, query: str) -> object:
    current = data
    for token in _tokenize_json_path(query):
        if isinstance(token, int):
            if not isinstance(current, list):
                raise ValueError(f"query index {token} applied to non-list value")
            if token < 0 or token >= len(current):
                raise ValueError(f"query index out of range: {token}")
            current = current[token]
            continue
        if not isinstance(current, dict):
            raise ValueError(f"query key '{token}' applied to non-object value")
        if token not in current:
            raise ValueError(f"query key not found: {token}")
        current = current[token]
    return current


def _tokenize_json_path(query: str) -> list[str | int]:
    tokens: list[str | int] = []
    for part in query.split("."):
        chunk = part.strip()
        if not chunk:
            raise ValueError("invalid query path")
        while "[" in chunk:
            left, rest = chunk.split("[", 1)
            if left:
                tokens.append(left)
            if "]" not in rest:
                raise ValueError("invalid query path")
            index_s, remainder = rest.split("]", 1)
            if not index_s.isdigit():
                raise ValueError("array index must be numeric")
            tokens.append(int(index_s))
            chunk = remainder
        if chunk:
            tokens.append(chunk)
    return tokens
