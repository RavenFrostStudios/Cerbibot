from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchestrator.observability.redaction import redact_text
from orchestrator.security.encryption import EnvelopeCipher
from orchestrator.security.taint import TaintedString


class AuditLogger:
    """Writes redacted JSONL records for local run auditing."""

    def __init__(self, path: str, cipher: EnvelopeCipher | None = None):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.cipher = cipher

    def write(self, event_type: str, payload: dict[str, Any]) -> None:
        redacted_payload, taint_metadata = _redact_payload(payload)
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "payload": redacted_payload,
            "taint": taint_metadata,
        }
        line = json.dumps(record, sort_keys=True)
        if self.cipher is not None:
            encrypted = self.cipher.encrypt_text(
                line,
                aad={"record_type": "audit", "orchestrator_version": "0.1.0"},
            )
            line = json.dumps({"encrypted": True, "payload": encrypted}, sort_keys=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


def _redact_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    redacted: dict[str, Any] = {}
    taint_entries: list[dict[str, str]] = []
    for key, value in payload.items():
        redacted_value, value_taint = _redact_value(value, field=key)
        redacted[key] = redacted_value
        taint_entries.extend(value_taint)
    return redacted, taint_entries


def _redact_value(value: Any, field: str) -> tuple[Any, list[dict[str, str]]]:
    if isinstance(value, TaintedString):
        return (
            redact_text(value.value),
            [
                {
                    "field": field,
                    "source": value.source,
                    "source_id": value.source_id,
                    "taint_level": value.taint_level,
                }
            ],
        )
    if isinstance(value, str):
        return redact_text(value), []
    if isinstance(value, list):
        out = []
        taint: list[dict[str, str]] = []
        for idx, item in enumerate(value):
            v, t = _redact_value(item, field=f"{field}[{idx}]")
            out.append(v)
            taint.extend(t)
        return out, taint
    if isinstance(value, dict):
        out_dict: dict[str, Any] = {}
        taint: list[dict[str, str]] = []
        for k, v in value.items():
            rv, rt = _redact_value(v, field=f"{field}.{k}")
            out_dict[k] = rv
            taint.extend(rt)
        return out_dict, taint
    return value, []
