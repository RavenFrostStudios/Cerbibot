from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

from orchestrator.security.taint import TaintedString, validate_for_tool_arg

URL_PATTERN = re.compile(r"https?://[^\s]+", re.IGNORECASE)
SAFE_QUERY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9\s,._:\-?!()]{0,199}")


def validate_url_arg(
    value: TaintedString | str,
    *,
    allowlist: list[str] | None = None,
    denylist: list[str] | None = None,
) -> str:
    url = validate_for_tool_arg(value, URL_PATTERN)
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()

    if denylist and any(host == denied.lower() or host.endswith(f".{denied.lower()}") for denied in denylist):
        raise ValueError(f"URL host denied by policy: {host}")

    if allowlist:
        allowed = any(host == allowed_host.lower() or host.endswith(f".{allowed_host.lower()}") for allowed_host in allowlist)
        if not allowed:
            raise ValueError(f"URL host not in allowlist: {host}")
    return url


def validate_path_arg(value: TaintedString | str, *, allowed_prefixes: list[str]) -> str:
    path_text = validate_for_tool_arg(value, r"[^\n\r\t]+")
    resolved = Path(path_text).expanduser().resolve()
    prefixes = [Path(prefix).expanduser().resolve() for prefix in allowed_prefixes]

    if not prefixes:
        raise ValueError("No allowed path prefixes configured")

    if not any(str(resolved).startswith(str(prefix)) for prefix in prefixes):
        raise ValueError(f"Path not allowed by policy: {resolved}")
    return str(resolved)


def validate_search_query_arg(value: TaintedString | str) -> str:
    query = validate_for_tool_arg(value, SAFE_QUERY_PATTERN)
    query = query.strip()
    if len(query) < 2:
        raise ValueError("Search query too short")
    return query


def validate_tool_args(
    *,
    tool_name: str,
    args: dict[str, TaintedString | str],
    url_allowlist: list[str] | None = None,
    url_denylist: list[str] | None = None,
    path_allow_prefixes: list[str] | None = None,
    arg_patterns: dict[str, str] | None = None,
) -> dict[str, str]:
    validated: dict[str, str] = {}

    for key, value in args.items():
        if arg_patterns and key in arg_patterns:
            validated[key] = validate_for_tool_arg(value, arg_patterns[key])
            continue
        if tool_name == "python_exec" and key == "code":
            validated[key] = validate_for_tool_arg(value, r"[\s\S]{1,12000}")
            continue
        if key in {"url", "uri"}:
            validated[key] = validate_url_arg(value, allowlist=url_allowlist, denylist=url_denylist)
        elif key in {"path", "file_path"}:
            validated[key] = validate_path_arg(value, allowed_prefixes=path_allow_prefixes or [])
        elif key in {"query", "search_query"}:
            validated[key] = validate_search_query_arg(value)
        else:
            validated[key] = validate_for_tool_arg(value, r"[^\n\r\t]{1,500}")

    return validated
