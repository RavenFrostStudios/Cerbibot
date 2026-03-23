from __future__ import annotations

import base64
import json
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from orchestrator.config import DataProtectionConfig

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ModuleNotFoundError:  # pragma: no cover
    AESGCM = None  # type: ignore[assignment]

try:
    from argon2.low_level import Type as Argon2Type
    from argon2.low_level import hash_secret_raw
except ModuleNotFoundError:  # pragma: no cover
    Argon2Type = None  # type: ignore[assignment]
    hash_secret_raw = None  # type: ignore[assignment]


class MasterKeyProvider(ABC):
    @abstractmethod
    def get_master_key(self) -> bytes:
        """Return a 32-byte master key."""


class PassphraseKeyProvider(MasterKeyProvider):
    def __init__(self, passphrase_env: str):
        self.passphrase_env = passphrase_env
        self._salt_path = _state_dir() / "master_key_salt.bin"

    def get_master_key(self) -> bytes:
        passphrase = os.getenv(self.passphrase_env)
        if not passphrase:
            raise RuntimeError(f"Missing passphrase env: {self.passphrase_env}")
        if hash_secret_raw is None or Argon2Type is None:
            raise RuntimeError("argon2-cffi is required for passphrase key provider")
        salt = self._load_or_create_salt()
        return hash_secret_raw(
            secret=passphrase.encode("utf-8"),
            salt=salt,
            time_cost=3,
            memory_cost=65536,
            parallelism=2,
            hash_len=32,
            type=Argon2Type.ID,
        )

    def _load_or_create_salt(self) -> bytes:
        self._salt_path.parent.mkdir(parents=True, exist_ok=True)
        if self._salt_path.exists():
            return self._salt_path.read_bytes()
        salt = os.urandom(16)
        self._salt_path.write_bytes(salt)
        return salt


class OSKeyringProvider(MasterKeyProvider):
    def __init__(self, service_name: str = "multi-mind-orchestrator"):
        self.service_name = service_name
        self.username = "master_key"
        self.fallback_path = _state_dir() / "keyring_fallback.json"

    def get_master_key(self) -> bytes:
        try:
            import keyring  # type: ignore

            existing = keyring.get_password(self.service_name, self.username)
            if existing:
                return base64.b64decode(existing.encode("utf-8"))
            key = os.urandom(32)
            keyring.set_password(self.service_name, self.username, base64.b64encode(key).decode("utf-8"))
            return key
        except Exception:
            return self._fallback_file_key()

    def _fallback_file_key(self) -> bytes:
        self.fallback_path.parent.mkdir(parents=True, exist_ok=True)
        if self.fallback_path.exists():
            data = json.loads(self.fallback_path.read_text(encoding="utf-8"))
            raw = data.get(self.username)
            if raw:
                return base64.b64decode(raw.encode("utf-8"))
        key = os.urandom(32)
        payload = {self.username: base64.b64encode(key).decode("utf-8")}
        self.fallback_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return key


def build_master_key_provider(config: DataProtectionConfig) -> MasterKeyProvider:
    if config.key_provider == "os_keyring":
        return OSKeyringProvider()
    return PassphraseKeyProvider(passphrase_env=config.passphrase_env)


def set_secret(name: str, value: str, *, service_name: str = "multi-mind-orchestrator") -> None:
    provider = OSKeyringProvider(service_name=service_name)
    try:
        import keyring  # type: ignore

        keyring.set_password(provider.service_name, name, value)
    except Exception:
        path = provider.fallback_path
        path.parent.mkdir(parents=True, exist_ok=True)
        encrypted = _encrypt_fallback_value(provider, name, value)
        payload: dict[str, Any] = {}
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
        payload[_secret_name(name)] = encrypted
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def get_secret(name: str, *, service_name: str = "multi-mind-orchestrator") -> str | None:
    provider = OSKeyringProvider(service_name=service_name)
    try:
        import keyring  # type: ignore

        value = keyring.get_password(provider.service_name, name)
        if value:
            return value
    except Exception:
        pass
    path = provider.fallback_path
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw = payload.get(_secret_name(name))
    if isinstance(raw, str):  # legacy plaintext fallback
        return raw or None
    if isinstance(raw, dict):
        return _decrypt_fallback_value(provider, name, raw)
    return None


def delete_secret(name: str, *, service_name: str = "multi-mind-orchestrator") -> bool:
    provider = OSKeyringProvider(service_name=service_name)
    deleted = False
    try:
        import keyring  # type: ignore

        keyring.delete_password(provider.service_name, name)
        deleted = True
    except Exception:
        pass
    path = provider.fallback_path
    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.pop(_secret_name(name), None) is not None:
            deleted = True
            path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return deleted


def has_secret(name: str, *, service_name: str = "multi-mind-orchestrator") -> bool:
    value = get_secret(name, service_name=service_name)
    return bool(value)


def _secret_name(name: str) -> str:
    return f"secret:{name}"


def _encrypt_fallback_value(provider: OSKeyringProvider, name: str, value: str) -> dict[str, str]:
    if AESGCM is None:
        return {"format": "plain-v0", "value": value}
    key = provider.get_master_key()
    aes = AESGCM(key)
    nonce = os.urandom(12)
    aad = f"mmo-secret:{name}".encode("utf-8")
    ciphertext = aes.encrypt(nonce, value.encode("utf-8"), aad)
    return {
        "format": "enc-v1",
        "nonce": base64.b64encode(nonce).decode("utf-8"),
        "ciphertext": base64.b64encode(ciphertext).decode("utf-8"),
    }


def _decrypt_fallback_value(provider: OSKeyringProvider, name: str, payload: dict[str, Any]) -> str | None:
    fmt = str(payload.get("format", "")).strip()
    if fmt == "plain-v0":
        raw = payload.get("value")
        return str(raw) if isinstance(raw, str) and raw else None
    if fmt != "enc-v1":
        return None
    if AESGCM is None:
        return None
    nonce_raw = payload.get("nonce")
    ciphertext_raw = payload.get("ciphertext")
    if not isinstance(nonce_raw, str) or not isinstance(ciphertext_raw, str):
        return None
    try:
        nonce = base64.b64decode(nonce_raw.encode("utf-8"))
        ciphertext = base64.b64decode(ciphertext_raw.encode("utf-8"))
        key = provider.get_master_key()
        aes = AESGCM(key)
        aad = f"mmo-secret:{name}".encode("utf-8")
        plaintext = aes.decrypt(nonce, ciphertext, aad)
        value = plaintext.decode("utf-8")
        return value if value else None
    except Exception:
        return None


def _state_dir() -> Path:
    env_dir = os.getenv("MMO_STATE_DIR")
    candidate = Path(env_dir).expanduser() if env_dir else Path("~/.mmo").expanduser()
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        return candidate
    except PermissionError:
        fallback = Path("/tmp/mmo")
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback
