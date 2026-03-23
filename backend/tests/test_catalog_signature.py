from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from orchestrator.skills import catalog


def test_curated_catalog_signature_verifies() -> None:
    ok, reason = catalog.verify_curated_catalog_signature()
    assert ok is True, reason


def test_curated_catalog_rejects_tampered_manifest(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    manifest = tmp_path / "catalog_manifest.json"
    signature = tmp_path / "catalog_manifest.json.sig.json"
    pub = tmp_path / "catalog_ed25519.pub.pem"
    keyring = tmp_path / "catalog_trusted_keys.json"

    shutil.copy2(catalog._CATALOG_MANIFEST_PATH, manifest)
    shutil.copy2(catalog._CATALOG_SIGNATURE_PATH, signature)
    shutil.copy2(catalog._CATALOG_PUBLIC_KEY_PATH, pub)
    keyring.write_text('{"public_keys":["catalog_ed25519.pub.pem"]}', encoding="utf-8")

    tampered = manifest.read_text(encoding="utf-8").replace("Repo Health Check", "Repo Health Hacked", 1)
    manifest.write_text(tampered, encoding="utf-8")

    monkeypatch.setattr(catalog, "_CATALOG_MANIFEST_PATH", manifest)
    monkeypatch.setattr(catalog, "_CATALOG_SIGNATURE_PATH", signature)
    monkeypatch.setattr(catalog, "_CATALOG_PUBLIC_KEY_PATH", pub)
    monkeypatch.setattr(catalog, "_CATALOG_KEYS_DIR", tmp_path)
    monkeypatch.setattr(catalog, "_CATALOG_TRUSTED_KEYS_FILE", keyring)

    ok, reason = catalog.verify_curated_catalog_signature()
    assert ok is False
    assert "Checksum mismatch" in reason

    with pytest.raises(RuntimeError, match="signature verification failed"):
        catalog.curated_skill_catalog({})
