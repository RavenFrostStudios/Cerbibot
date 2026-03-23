from __future__ import annotations

import json
import re
from dataclasses import dataclass


@dataclass(slots=True)
class ParsedStructured:
    data: dict
    valid: bool
    error: str | None = None


def parse_structured_output(text: str, required_keys: list[str]) -> ParsedStructured:
    """Parse a JSON object from model output and validate required keys."""
    parse_errors: list[str] = []
    candidates = [text.strip(), _extract_json_from_fence(text)]
    candidates.extend(_extract_balanced_json_objects(text, limit=6))
    seen: set[str] = set()

    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError as exc:
            parse_errors.append(exc.msg)
            continue
        if not isinstance(data, dict):
            parse_errors.append("JSON root is not an object")
            continue
        missing = [k for k in required_keys if k not in data]
        if missing:
            return ParsedStructured(data=data, valid=False, error=f"Missing required keys: {missing}")
        return ParsedStructured(data=data, valid=True)

    fallback = _fallback_structure(text, required_keys)
    return ParsedStructured(
        data=fallback,
        valid=False,
        error=f"Unable to parse structured JSON ({'; '.join(parse_errors) or 'no candidate found'})",
    )


def _extract_balanced_json_object(text: str) -> str | None:
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _extract_balanced_json_objects(text: str, limit: int = 6) -> list[str]:
    out: list[str] = []
    if not text or limit <= 0:
        return out
    for i, char in enumerate(text):
        if char != "{":
            continue
        candidate = _extract_balanced_json_object(text[i:])
        if candidate:
            out.append(candidate)
            if len(out) >= limit:
                break
    return out


def _extract_json_from_fence(text: str) -> str | None:
    match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if not match:
        return None
    return match.group(1).strip()


def _fallback_structure(text: str, required_keys: list[str]) -> dict:
    fallback = {key: text for key in required_keys}
    fallback["raw_text"] = text
    return fallback
