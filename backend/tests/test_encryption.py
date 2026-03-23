from __future__ import annotations

import json
import sqlite3

import pytest

pytest.importorskip("cryptography")
pytest.importorskip("argon2")

from orchestrator.budgets import BudgetTracker
from orchestrator.config import BudgetConfig, DataProtectionConfig
from orchestrator.memory.store import MemoryStore
from orchestrator.security.encryption import build_envelope_cipher


def _cipher() -> object:
    cfg = DataProtectionConfig(encrypt_at_rest=True, key_provider="passphrase", passphrase_env="MMO_MASTER_PASSPHRASE")
    return build_envelope_cipher(cfg)


def test_envelope_cipher_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_MASTER_PASSPHRASE", "test-passphrase")
    monkeypatch.setenv("MMO_STATE_DIR", "/tmp/mmo-test")
    cipher = _cipher()
    assert cipher is not None
    token = cipher.encrypt_text("hello", aad={"record_type": "test", "orchestrator_version": "0.1.0"})
    assert cipher.decrypt_text(token) == "hello"


def test_budget_tracker_encrypted_usage_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("MMO_MASTER_PASSPHRASE", "test-passphrase")
    monkeypatch.setenv("MMO_STATE_DIR", str(tmp_path / "state"))
    cipher = _cipher()
    tracker = BudgetTracker(
        BudgetConfig(
            session_usd_cap=5.0,
            daily_usd_cap=5.0,
            monthly_usd_cap=5.0,
            usage_file=str(tmp_path / "usage.json"),
        ),
        cipher=cipher,
    )
    tracker.record_cost("openai", 0.1, 10, 10)
    raw = json.loads((tmp_path / "usage.json").read_text(encoding="utf-8"))
    assert raw.get("encrypted") is True

    tracker2 = BudgetTracker(
        BudgetConfig(
            session_usd_cap=5.0,
            daily_usd_cap=5.0,
            monthly_usd_cap=5.0,
            usage_file=str(tmp_path / "usage.json"),
        ),
        cipher=cipher,
    )
    assert tracker2.state().daily_spend >= 0.1


def test_memory_store_encrypts_statement(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("MMO_MASTER_PASSPHRASE", "test-passphrase")
    monkeypatch.setenv("MMO_STATE_DIR", str(tmp_path / "state"))
    cipher = _cipher()
    store = MemoryStore(str(tmp_path / "memory.db"), cipher=cipher)
    mem_id = store.add(
        statement="Top secret preference",
        source_type="summary",
        source_ref="run:1",
        confidence=0.7,
        ttl_days=30,
    )
    conn = sqlite3.connect(tmp_path / "memory.db")
    row = conn.execute("SELECT statement FROM memories WHERE id = ?", (mem_id,)).fetchone()
    assert row is not None
    assert "Top secret preference" not in row[0]
    rows = store.list_records()
    assert rows[0].statement == "Top secret preference"
