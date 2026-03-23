from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("cryptography")

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from orchestrator.skills.registry import install_skill
from orchestrator.skills.signing import compute_skill_checksum, sign_skill, verify_skill_signature


def _write_keys(tmp_path: Path) -> tuple[Path, Path]:
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    priv_path = tmp_path / "skill_signing_private.pem"
    pub_path = tmp_path / "skill_signing_public.pem"
    priv_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    pub_path.write_bytes(
        public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return priv_path, pub_path


def _write_skill(path: Path, text: str = "hello") -> Path:
    skill = path / "demo.workflow.yaml"
    skill.write_text(
        f"""
name: signed_demo
manifest:
  purpose: "Signing flow test skill."
  tools: [system_info]
  data_scope: ["none"]
  permissions: ["model_call"]
  approval_policy: draft_only
  rate_limits:
    actions_per_hour: 10
  budgets:
    usd_cap: 1.0
  kill_switch:
    enabled: true
  audit_sink: "audit.jsonl"
  failure_mode: "safe_abort"
steps:
  - model_call: "{text}"
    output: out
""",
        encoding="utf-8",
    )
    return skill


def test_skill_sign_and_verify_roundtrip(tmp_path: Path) -> None:
    priv, pub = _write_keys(tmp_path)
    skill = _write_skill(tmp_path)
    sig = sign_skill(str(skill), private_key_path=str(priv))
    assert sig.exists()

    ok, reason = verify_skill_signature(str(skill), public_key_paths=[str(pub)])
    assert ok is True
    assert "Verified with" in reason


def test_skill_verify_fails_on_tamper(tmp_path: Path) -> None:
    priv, pub = _write_keys(tmp_path)
    skill = _write_skill(tmp_path, text="before")
    sign_skill(str(skill), private_key_path=str(priv))
    skill.write_text(
        """
name: signed_demo
manifest:
  purpose: "Signing flow test skill."
  tools: [system_info]
  data_scope: ["none"]
  permissions: ["model_call"]
  approval_policy: draft_only
  rate_limits:
    actions_per_hour: 10
  budgets:
    usd_cap: 1.0
  kill_switch:
    enabled: true
  audit_sink: "audit.jsonl"
  failure_mode: "safe_abort"
steps:
  - model_call: "after"
    output: out
""",
        encoding="utf-8",
    )
    ok, reason = verify_skill_signature(str(skill), public_key_paths=[str(pub)])
    assert ok is False
    assert "Checksum mismatch" in reason


def test_install_skill_requires_valid_signature(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MMO_STATE_DIR", str(tmp_path / "state"))
    priv, pub = _write_keys(tmp_path)
    source = tmp_path / "source"
    source.mkdir(parents=True)
    skill = _write_skill(source, text="signed")
    sign_skill(str(skill), private_key_path=str(priv))

    record = install_skill(
        str(skill),
        require_signature=True,
        trusted_public_keys=[str(pub)],
    )
    assert record.signature_verified is True
    assert record.checksum.startswith("sha256:")


def test_checksum_stable_for_file(tmp_path: Path) -> None:
    skill = _write_skill(tmp_path)
    c1 = compute_skill_checksum(str(skill))
    c2 = compute_skill_checksum(str(skill))
    assert c1 == c2
