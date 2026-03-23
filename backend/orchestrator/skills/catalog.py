from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestrator.skills.signing import verify_skill_signature


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    id: str
    title: str
    description: str
    trust: str
    tested: str
    risk_level: str
    workflow_text: str


_CATALOG_MANIFEST_PATH = Path(__file__).resolve().parent / "catalog_manifest.json"
_CATALOG_SIGNATURE_PATH = Path(__file__).resolve().parent / "catalog_manifest.json.sig.json"
_CATALOG_PUBLIC_KEY_PATH = Path(__file__).resolve().parent / "keys" / "catalog_ed25519.pub.pem"
_CATALOG_KEYS_DIR = Path(__file__).resolve().parent / "keys"
_CATALOG_TRUSTED_KEYS_FILE = _CATALOG_KEYS_DIR / "catalog_trusted_keys.json"


def _trusted_catalog_public_keys() -> list[str]:
    out: list[str] = []
    if _CATALOG_TRUSTED_KEYS_FILE.exists():
        try:
            raw = json.loads(_CATALOG_TRUSTED_KEYS_FILE.read_text(encoding="utf-8"))
            rows = raw.get("public_keys", []) if isinstance(raw, dict) else []
            if isinstance(rows, list):
                for item in rows:
                    key_ref = str(item).strip()
                    if not key_ref:
                        continue
                    candidate = Path(key_ref)
                    if not candidate.is_absolute():
                        candidate = (_CATALOG_KEYS_DIR / key_ref).resolve()
                    else:
                        candidate = candidate.expanduser().resolve()
                    if candidate.exists():
                        out.append(str(candidate))
        except Exception:
            pass
    if not out and _CATALOG_PUBLIC_KEY_PATH.exists():
        # Legacy fallback for older installs.
        out.append(str(_CATALOG_PUBLIC_KEY_PATH))
    return list(dict.fromkeys(out))


def verify_curated_catalog_signature() -> tuple[bool, str]:
    if not _CATALOG_MANIFEST_PATH.exists():
        return False, f"Catalog manifest not found: {_CATALOG_MANIFEST_PATH}"
    if not _CATALOG_SIGNATURE_PATH.exists():
        return False, f"Catalog signature file not found: {_CATALOG_SIGNATURE_PATH}"
    trusted_public_keys = _trusted_catalog_public_keys()
    if not trusted_public_keys:
        return False, "No trusted catalog public keys configured"
    return verify_skill_signature(
        str(_CATALOG_MANIFEST_PATH),
        public_key_paths=trusted_public_keys,
        signature_path=str(_CATALOG_SIGNATURE_PATH),
    )


def _load_catalog_entries_from_manifest() -> list[CatalogEntry]:
    raw = json.loads(_CATALOG_MANIFEST_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Catalog manifest root must be an object")
    entries_raw = raw.get("entries")
    if not isinstance(entries_raw, list) or not entries_raw:
        raise ValueError("Catalog manifest must contain a non-empty 'entries' list")

    out: list[CatalogEntry] = []
    for idx, item in enumerate(entries_raw, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Catalog entry {idx} must be an object")
        entry = CatalogEntry(
            id=str(item.get("id", "")).strip(),
            title=str(item.get("title", "")).strip(),
            description=str(item.get("description", "")).strip(),
            trust=str(item.get("trust", "")).strip(),
            tested=str(item.get("tested", "")).strip(),
            risk_level=str(item.get("risk_level", "")).strip(),
            workflow_text=str(item.get("workflow_text", "")),
        )
        if not entry.id:
            raise ValueError(f"Catalog entry {idx} missing id")
        if entry.tested not in {"smoke", "schema"}:
            raise ValueError(f"Catalog entry {entry.id} has invalid tested value: {entry.tested}")
        if entry.risk_level not in {"low", "medium", "high"}:
            raise ValueError(f"Catalog entry {entry.id} has invalid risk_level: {entry.risk_level}")
        out.append(entry)
    return out


def curated_skill_catalog(discovered: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    discovered = discovered or {}
    ok, reason = verify_curated_catalog_signature()
    if not ok:
        raise RuntimeError(f"Curated catalog signature verification failed: {reason}")

    entries = _load_catalog_entries_from_manifest()
    out: list[dict[str, Any]] = []
    for entry in entries:
        record = discovered.get(entry.id)
        out.append(
            {
                "id": entry.id,
                "title": entry.title,
                "description": entry.description,
                "trust": entry.trust,
                "tested": entry.tested,
                "risk_level": entry.risk_level,
                "workflow_text": entry.workflow_text,
                "official": entry.trust == "mmy-curated",
                "installed": bool(record is not None),
                "enabled": bool(getattr(record, "enabled", False)) if record is not None else False,
                "signature_verified": True if entry.trust == "mmy-curated" else (
                    bool(getattr(record, "signature_verified", False)) if record is not None else False
                ),
                "checksum": str(getattr(record, "checksum", "")) if record is not None else "",
            }
        )
    return out
