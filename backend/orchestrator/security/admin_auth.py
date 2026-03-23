from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from orchestrator.config import DataProtectionConfig
from orchestrator.security.encryption import build_envelope_cipher


_PH = PasswordHasher()


def admin_password_status() -> dict[str, object]:
    payload = _load_payload()
    return {
        "configured": bool(payload.get("password_hash")),
        "updated_at": payload.get("updated_at"),
    }


def set_admin_password(password: str) -> None:
    pwd = password.strip()
    if len(pwd) < 8:
        raise ValueError("admin password must be at least 8 characters")
    payload = _load_payload()
    payload["password_hash"] = _PH.hash(pwd)
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save_payload(payload)


def verify_admin_password(password: str) -> bool:
    payload = _load_payload()
    stored = str(payload.get("password_hash", "")).strip()
    if not stored:
        return False
    try:
        ok = _PH.verify(stored, password)
        if ok and _PH.check_needs_rehash(stored):
            payload["password_hash"] = _PH.hash(password)
            payload["updated_at"] = datetime.now(timezone.utc).isoformat()
            _save_payload(payload)
        return bool(ok)
    except VerifyMismatchError:
        return False
    except Exception:
        return False


def _payload_path() -> Path:
    raw = os.getenv("MMO_ADMIN_AUTH_FILE", "~/.mmo/admin_auth.json")
    return Path(raw).expanduser()


def _load_payload() -> dict[str, object]:
    path = _payload_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        if data.get("format") == "mmo-admin-auth-v2" and isinstance(data.get("encrypted"), str):
            cipher = _admin_cipher()
            if cipher is None:
                return {}
            try:
                raw = cipher.decrypt_text(str(data["encrypted"]))
                payload = json.loads(raw)
                return payload if isinstance(payload, dict) else {}
            except Exception:
                return {}
        return data
    except Exception:
        return {}


def _save_payload(payload: dict[str, object]) -> None:
    path = _payload_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if _encryption_enabled():
        cipher = _admin_cipher()
        if cipher is None:
            raise RuntimeError("admin auth encryption requested but encryption backend is unavailable")
        token = cipher.encrypt_text(
            json.dumps(payload, sort_keys=True),
            aad={"purpose": "admin_auth", "path": str(path)},
        )
        to_write: dict[str, object] = {"format": "mmo-admin-auth-v2", "encrypted": token}
    else:
        to_write = payload
    path.write_text(json.dumps(to_write, indent=2, sort_keys=True), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _encryption_enabled() -> bool:
    raw = str(os.getenv("MMO_ADMIN_AUTH_ENCRYPT", "")).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _admin_cipher():
    config = DataProtectionConfig(
        encrypt_at_rest=True,
        key_provider="os_keyring",
        passphrase_env="MMO_MASTER_PASSPHRASE",
    )
    return build_envelope_cipher(config)
