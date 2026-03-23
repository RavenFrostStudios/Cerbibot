from __future__ import annotations

import hashlib
import hmac
import json
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from orchestrator.config import ArtifactsConfig
from orchestrator.security.encryption import EnvelopeCipher


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class ArtifactSummary:
    request_id: str
    started_at: str
    mode: str
    query_preview: str
    cost: float
    path: str


class ArtifactStore:
    def __init__(self, config: ArtifactsConfig, *, cipher: EnvelopeCipher | None):
        self.config = config
        self.cipher = cipher
        self.root = Path(config.directory).expanduser()
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError:
            self.root = Path(tempfile.gettempdir()) / "mmo_artifacts"
            self.root.mkdir(parents=True, exist_ok=True)
        self.chain_file = self.root / ".chain_state.json"

    def save(self, artifact: dict[str, Any]) -> str:
        self._cleanup()
        request_id = str(artifact.get("request_id") or "unknown")
        started_at = str(artifact.get("started_at") or _utcnow().isoformat())
        chain_prev = self._load_chain_state().get("last_hash", "")
        canonical = json.dumps(artifact, sort_keys=True, separators=(",", ":"))
        integrity_hash = hashlib.sha256(f"{chain_prev}:{canonical}".encode("utf-8")).hexdigest()
        mac_key = self._mac_key()
        integrity_hmac = hmac.new(mac_key, canonical.encode("utf-8"), hashlib.sha256).hexdigest()

        payload = {
            "meta": {
                "request_id": request_id,
                "started_at": started_at,
                "integrity_hash": integrity_hash,
                "integrity_prev": chain_prev,
                "integrity_hmac": integrity_hmac,
                "encrypted": self.cipher is not None,
            },
            "artifact": artifact,
        }

        target = self.root / f"{request_id}.json"
        if self.cipher is None:
            target.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        else:
            encrypted = self.cipher.maybe_encrypt_json(payload, aad={"record_type": "artifact", "request_id": request_id})
            target.write_text(json.dumps({"encrypted": True, "payload": encrypted}), encoding="utf-8")
        self._save_chain_state({"last_hash": integrity_hash, "last_request_id": request_id})
        return str(target)

    def load(self, request_id: str) -> dict[str, Any]:
        path = self.root / f"{request_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"Artifact not found: {request_id}")
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and raw.get("encrypted") is True:
            if self.cipher is None:
                raise RuntimeError("Artifact is encrypted but encryption is not configured")
            payload = raw.get("payload")
            if not isinstance(payload, str):
                raise RuntimeError("Invalid encrypted artifact payload")
            raw = self.cipher.maybe_decrypt_json(payload)
        if not isinstance(raw, dict) or "artifact" not in raw or "meta" not in raw:
            raise RuntimeError("Invalid artifact format")
        self._verify_integrity(raw)
        return raw

    def list_summaries(self, limit: int = 50) -> list[ArtifactSummary]:
        entries: list[ArtifactSummary] = []
        files = sorted(self.root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in files[:limit]:
            if path.name.startswith("."):
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("encrypted") is True:
                    if self.cipher is None:
                        continue
                    payload = data.get("payload")
                    if not isinstance(payload, str):
                        continue
                    data = self.cipher.maybe_decrypt_json(payload)
                artifact = data.get("artifact", {})
                entries.append(
                    ArtifactSummary(
                        request_id=str(artifact.get("request_id", path.stem)),
                        started_at=str(artifact.get("started_at", "")),
                        mode=str(artifact.get("mode", "")),
                        query_preview=str(artifact.get("query", ""))[:80],
                        cost=float((artifact.get("result") or {}).get("cost", 0.0)),
                        path=str(path),
                    )
                )
            except Exception:
                continue
        return entries

    def export(self, request_id: str, *, output_path: str, fmt: str) -> str:
        raw = self.load(request_id)
        target = Path(output_path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        if fmt == "json":
            target.write_text(json.dumps(raw, indent=2, sort_keys=True), encoding="utf-8")
        elif fmt == "yaml":
            import yaml

            target.write_text(yaml.safe_dump(raw, sort_keys=True), encoding="utf-8")
        else:
            raise ValueError(f"Unsupported format: {fmt}")
        return str(target)

    def delete(self, request_id: str) -> bool:
        path = self.root / f"{request_id}.json"
        if not path.exists():
            return False
        try:
            path.unlink(missing_ok=True)
            return True
        except Exception:
            return False

    def delete_many(self, *, older_than_days: int | None = None, limit: int | None = None) -> list[str]:
        files = sorted(self.root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if older_than_days is not None:
            cutoff = _utcnow() - timedelta(days=max(1, int(older_than_days)))
        else:
            cutoff = None

        deleted: list[str] = []
        for path in files:
            if path.name.startswith("."):
                continue
            try:
                if cutoff is not None:
                    modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
                    if modified >= cutoff:
                        continue
                request_id = path.stem
                path.unlink(missing_ok=True)
                deleted.append(request_id)
                if limit is not None and limit > 0 and len(deleted) >= limit:
                    break
            except Exception:
                continue
        return deleted

    def _verify_integrity(self, payload: dict[str, Any]) -> None:
        meta = payload.get("meta", {})
        artifact = payload.get("artifact", {})
        canonical = json.dumps(artifact, sort_keys=True, separators=(",", ":"))
        expected_mac = hmac.new(self._mac_key(), canonical.encode("utf-8"), hashlib.sha256).hexdigest()
        got_mac = str(meta.get("integrity_hmac", ""))
        if not hmac.compare_digest(expected_mac, got_mac):
            raise RuntimeError("Artifact integrity check failed (HMAC mismatch)")

    def _cleanup(self) -> None:
        cutoff = _utcnow() - timedelta(days=self.config.retention_days)
        for path in self.root.glob("*.json"):
            try:
                if datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc) < cutoff:
                    path.unlink(missing_ok=True)
            except Exception:
                continue

    def _load_chain_state(self) -> dict[str, str]:
        if not self.chain_file.exists():
            return {"last_hash": "", "last_request_id": ""}
        try:
            raw = json.loads(self.chain_file.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return {"last_hash": str(raw.get("last_hash", "")), "last_request_id": str(raw.get("last_request_id", ""))}
        except Exception:
            pass
        return {"last_hash": "", "last_request_id": ""}

    def _save_chain_state(self, state: dict[str, str]) -> None:
        self.chain_file.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")

    def _mac_key(self) -> bytes:
        if self.cipher is not None:
            return hashlib.sha256(self.cipher.key_provider.get_master_key()).digest()
        return hashlib.sha256(str(self.root).encode("utf-8")).digest()
