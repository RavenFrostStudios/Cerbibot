from __future__ import annotations

import json
from pathlib import Path

from orchestrator.config import ArtifactsConfig, DataProtectionConfig
from orchestrator.observability.artifacts import ArtifactStore
from orchestrator.security.encryption import build_envelope_cipher


def test_artifact_store_save_load_and_integrity(tmp_path: Path) -> None:
    store = ArtifactStore(ArtifactsConfig(enabled=True, directory=str(tmp_path / "artifacts"), retention_days=30), cipher=None)
    store.save(
        {
            "request_id": "req-1",
            "started_at": "2026-02-10T00:00:00Z",
            "mode": "single",
            "query": "hello",
            "result": {"answer": "world", "cost": 0.01},
        }
    )
    loaded = store.load("req-1")
    assert loaded["artifact"]["request_id"] == "req-1"
    assert loaded["meta"]["integrity_hash"]


def test_artifact_store_list_and_export(tmp_path: Path) -> None:
    store = ArtifactStore(ArtifactsConfig(enabled=True, directory=str(tmp_path / "artifacts"), retention_days=30), cipher=None)
    store.save(
        {
            "request_id": "req-2",
            "started_at": "2026-02-10T00:00:00Z",
            "mode": "single",
            "query": "test export",
            "result": {"answer": "ok", "cost": 0.02},
        }
    )
    rows = store.list_summaries(limit=5)
    assert rows and rows[0].request_id == "req-2"
    out = store.export("req-2", output_path=str(tmp_path / "out.json"), fmt="json")
    assert Path(out).exists()


def test_artifact_store_encrypts_payload_at_rest(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MMO_STATE_DIR", str(tmp_path / "state"))
    cipher = build_envelope_cipher(DataProtectionConfig(encrypt_at_rest=True, key_provider="os_keyring"))
    assert cipher is not None
    store = ArtifactStore(ArtifactsConfig(enabled=True, directory=str(tmp_path / "artifacts"), retention_days=30), cipher=cipher)
    store.save(
        {
            "request_id": "req-enc",
            "started_at": "2026-02-10T00:00:00Z",
            "mode": "single",
            "query": "encrypt me",
            "result": {"answer": "ok", "cost": 0.01},
        }
    )
    raw = json.loads((tmp_path / "artifacts" / "req-enc.json").read_text(encoding="utf-8"))
    assert raw.get("encrypted") is True
    assert isinstance(raw.get("payload"), str)
    assert "artifact" not in raw
    loaded = store.load("req-enc")
    assert loaded["artifact"]["request_id"] == "req-enc"
