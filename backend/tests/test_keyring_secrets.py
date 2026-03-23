from __future__ import annotations

import json
import sys
import types

from orchestrator.security.keyring import get_secret, has_secret, set_secret


def test_fallback_secret_storage_is_not_plaintext(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MMO_STATE_DIR", str(tmp_path))

    fake_keyring = types.ModuleType("keyring")

    def _fail(*_args, **_kwargs):
        raise RuntimeError("backend unavailable")

    fake_keyring.set_password = _fail  # type: ignore[attr-defined]
    fake_keyring.get_password = _fail  # type: ignore[attr-defined]
    fake_keyring.delete_password = _fail  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "keyring", fake_keyring)

    set_secret("GOOGLE_API_KEY", "abc123")
    assert get_secret("GOOGLE_API_KEY") == "abc123"
    assert has_secret("GOOGLE_API_KEY") is True

    fallback_path = tmp_path / "keyring_fallback.json"
    payload = json.loads(fallback_path.read_text(encoding="utf-8"))
    stored = payload.get("secret:GOOGLE_API_KEY")
    assert isinstance(stored, dict)
    assert stored.get("format") == "enc-v1"
    assert "abc123" not in fallback_path.read_text(encoding="utf-8")
