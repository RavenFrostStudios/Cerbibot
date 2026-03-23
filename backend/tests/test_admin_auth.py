from __future__ import annotations

from pathlib import Path

from orchestrator.security.admin_auth import (
    admin_password_status,
    set_admin_password,
    verify_admin_password,
)


def test_admin_auth_plaintext_roundtrip(monkeypatch, tmp_path: Path) -> None:
    auth_file = tmp_path / "admin_auth.json"
    monkeypatch.setenv("MMO_ADMIN_AUTH_FILE", str(auth_file))
    monkeypatch.delenv("MMO_ADMIN_AUTH_ENCRYPT", raising=False)

    assert admin_password_status()["configured"] is False
    set_admin_password("ComplexPass123")
    status = admin_password_status()
    assert status["configured"] is True
    assert verify_admin_password("ComplexPass123") is True
    assert verify_admin_password("wrong") is False


def test_admin_auth_encrypted_roundtrip(monkeypatch, tmp_path: Path) -> None:
    auth_file = tmp_path / "admin_auth.json"
    state_dir = tmp_path / "state"
    monkeypatch.setenv("MMO_ADMIN_AUTH_FILE", str(auth_file))
    monkeypatch.setenv("MMO_ADMIN_AUTH_ENCRYPT", "1")
    monkeypatch.setenv("MMO_STATE_DIR", str(state_dir))

    set_admin_password("ComplexPass123")
    raw = auth_file.read_text(encoding="utf-8")
    assert "password_hash" not in raw
    assert "mmo-admin-auth-v2" in raw
    assert verify_admin_password("ComplexPass123") is True
    assert verify_admin_password("wrong") is False
