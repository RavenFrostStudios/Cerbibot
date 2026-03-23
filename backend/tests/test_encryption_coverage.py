from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("cryptography")
pytest.importorskip("argon2")

from orchestrator.budgets import BudgetConfig, BudgetTracker
from orchestrator.config import ArtifactsConfig, DataProtectionConfig
from orchestrator.memory.store import MemoryStore
from orchestrator.observability.artifacts import ArtifactStore
from orchestrator.observability.audit import AuditLogger
from orchestrator.security.encryption import build_envelope_cipher
from orchestrator.server import _load_sessions_from_disk, _read_recent_audit_events, _save_sessions_to_disk
from orchestrator.session import SessionManager


def _passphrase_cipher(monkeypatch: pytest.MonkeyPatch, *, state_dir: Path, passphrase: str):
    monkeypatch.setenv("MMO_STATE_DIR", str(state_dir))
    monkeypatch.setenv("MMO_MASTER_PASSPHRASE", passphrase)
    cfg = DataProtectionConfig(encrypt_at_rest=True, key_provider="passphrase", passphrase_env="MMO_MASTER_PASSPHRASE")
    cipher = build_envelope_cipher(cfg)
    assert cipher is not None
    return cipher


def test_sessions_encrypted_roundtrip_and_key_unavailable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sessions_file = tmp_path / "sessions_store.json"
    state_dir = tmp_path / "state"
    cipher_a = _passphrase_cipher(monkeypatch, state_dir=state_dir, passphrase="alpha")

    manager = SessionManager(max_context_tokens=8000)
    manager.add(role="user", content="hello")
    manager.add(role="assistant", content="world")
    _save_sessions_to_disk({"s1": manager}, sessions_file, cipher_a)

    raw = json.loads(sessions_file.read_text(encoding="utf-8"))
    assert raw.get("format") == "mmo-sessions-v1"
    assert isinstance(raw.get("encrypted"), str)
    assert "sessions" not in raw

    restored = _load_sessions_from_disk(sessions_file, cipher_a)
    assert "s1" in restored
    assert restored["s1"].export()[-1]["content"] == "world"

    cipher_b = _passphrase_cipher(monkeypatch, state_dir=state_dir, passphrase="beta")
    locked_out = _load_sessions_from_disk(sessions_file, cipher_b)
    assert locked_out == {}


def test_usage_file_encrypted_and_wrong_key_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    usage_file = tmp_path / "usage.json"
    state_dir = tmp_path / "state"
    cipher_a = _passphrase_cipher(monkeypatch, state_dir=state_dir, passphrase="alpha")
    tracker = BudgetTracker(
        BudgetConfig(
            session_usd_cap=5.0,
            daily_usd_cap=5.0,
            monthly_usd_cap=5.0,
            usage_file=str(usage_file),
        ),
        cipher=cipher_a,
    )
    tracker.record_cost("google", 0.1, 10, 20)
    raw = json.loads(usage_file.read_text(encoding="utf-8"))
    assert raw.get("encrypted") is True

    cipher_b = _passphrase_cipher(monkeypatch, state_dir=state_dir, passphrase="beta")
    with pytest.raises(Exception):
        BudgetTracker(
            BudgetConfig(
                session_usd_cap=5.0,
                daily_usd_cap=5.0,
                monthly_usd_cap=5.0,
                usage_file=str(usage_file),
            ),
            cipher=cipher_b,
        )


def test_artifact_file_encrypted_and_wrong_key_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    cipher_a = _passphrase_cipher(monkeypatch, state_dir=state_dir, passphrase="alpha")
    store = ArtifactStore(
        ArtifactsConfig(enabled=True, directory=str(tmp_path / "artifacts"), retention_days=30),
        cipher=cipher_a,
    )
    store.save(
        {
            "request_id": "req-1",
            "started_at": "2026-02-12T00:00:00Z",
            "mode": "single",
            "query": "hello",
            "result": {"answer": "world", "cost": 0.01},
        }
    )
    raw = json.loads((tmp_path / "artifacts" / "req-1.json").read_text(encoding="utf-8"))
    assert raw.get("encrypted") is True

    cipher_b = _passphrase_cipher(monkeypatch, state_dir=state_dir, passphrase="beta")
    locked_store = ArtifactStore(
        ArtifactsConfig(enabled=True, directory=str(tmp_path / "artifacts"), retention_days=30),
        cipher=cipher_b,
    )
    with pytest.raises(Exception):
        locked_store.load("req-1")


def test_memory_encrypted_and_wrong_key_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    db_path = tmp_path / "memory.db"
    cipher_a = _passphrase_cipher(monkeypatch, state_dir=state_dir, passphrase="alpha")
    store = MemoryStore(str(db_path), cipher=cipher_a)
    store.add(
        statement="confidential note",
        source_type="summary",
        source_ref="run:1",
        confidence=0.9,
        ttl_days=30,
    )

    cipher_b = _passphrase_cipher(monkeypatch, state_dir=state_dir, passphrase="beta")
    locked_store = MemoryStore(str(db_path), cipher=cipher_b)
    with pytest.raises(Exception):
        locked_store.list_records(limit=10)


def test_audit_file_encrypted_roundtrip_and_wrong_key_hidden(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    audit_path = tmp_path / "audit.jsonl"
    cipher_a = _passphrase_cipher(monkeypatch, state_dir=state_dir, passphrase="alpha")
    logger = AuditLogger(str(audit_path), cipher=cipher_a)
    logger.write("security_check", {"reason": "ok"})

    line = audit_path.read_text(encoding="utf-8").strip()
    raw_line = json.loads(line)
    assert raw_line.get("encrypted") is True
    assert isinstance(raw_line.get("payload"), str)

    events = _read_recent_audit_events(audit_path, cipher_a, limit=10)
    assert len(events) == 1
    assert events[0]["event_type"] == "security_check"

    cipher_b = _passphrase_cipher(monkeypatch, state_dir=state_dir, passphrase="beta")
    blocked = _read_recent_audit_events(audit_path, cipher_b, limit=10)
    assert blocked == []
