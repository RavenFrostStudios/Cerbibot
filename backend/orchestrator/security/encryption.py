from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from typing import Any

from orchestrator.config import DataProtectionConfig
from orchestrator.security.keyring import MasterKeyProvider, build_master_key_provider

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ModuleNotFoundError:  # pragma: no cover
    AESGCM = None  # type: ignore[assignment]


@dataclass(slots=True)
class EnvelopeCipher:
    key_provider: MasterKeyProvider

    def _require_aesgcm(self):
        if AESGCM is None:
            raise RuntimeError("cryptography is required for encrypt_at_rest (missing AESGCM)")
        return AESGCM

    def encrypt_record(self, plaintext: bytes, *, aad: dict[str, str]) -> dict[str, str]:
        aesgcm_cls = self._require_aesgcm()
        master_key = self.key_provider.get_master_key()
        dek = os.urandom(32)
        data_nonce = os.urandom(12)
        wrap_nonce = os.urandom(12)

        aad_bytes = json.dumps(aad, sort_keys=True).encode("utf-8")
        wrap_aad = b"mmo-dek-wrap-v1"
        ciphertext = aesgcm_cls(dek).encrypt(data_nonce, plaintext, aad_bytes)
        wrapped_dek = aesgcm_cls(master_key).encrypt(wrap_nonce, dek, wrap_aad)

        return {
            "wrapped_dek": base64.b64encode(wrapped_dek).decode("utf-8"),
            "wrap_nonce": base64.b64encode(wrap_nonce).decode("utf-8"),
            "nonce": base64.b64encode(data_nonce).decode("utf-8"),
            "ciphertext": base64.b64encode(ciphertext).decode("utf-8"),
            "aad": json.dumps(aad, sort_keys=True),
            "format": "mmo-envelope-v1",
        }

    def decrypt_record(self, payload: dict[str, str]) -> bytes:
        aesgcm_cls = self._require_aesgcm()
        master_key = self.key_provider.get_master_key()

        wrapped_dek = base64.b64decode(payload["wrapped_dek"].encode("utf-8"))
        wrap_nonce = base64.b64decode(payload["wrap_nonce"].encode("utf-8"))
        data_nonce = base64.b64decode(payload["nonce"].encode("utf-8"))
        ciphertext = base64.b64decode(payload["ciphertext"].encode("utf-8"))
        aad_json = payload.get("aad", "{}")
        aad_bytes = aad_json.encode("utf-8")

        dek = aesgcm_cls(master_key).decrypt(wrap_nonce, wrapped_dek, b"mmo-dek-wrap-v1")
        return aesgcm_cls(dek).decrypt(data_nonce, ciphertext, aad_bytes)

    def encrypt_text(self, text: str, *, aad: dict[str, str]) -> str:
        payload = self.encrypt_record(text.encode("utf-8"), aad=aad)
        return json.dumps(payload, sort_keys=True)

    def decrypt_text(self, token: str) -> str:
        payload = json.loads(token)
        return self.decrypt_record(payload).decode("utf-8")

    def maybe_encrypt_json(self, data: dict[str, Any], *, aad: dict[str, str]) -> str:
        return self.encrypt_text(json.dumps(data, sort_keys=True), aad=aad)

    def maybe_decrypt_json(self, raw: str) -> dict[str, Any]:
        return json.loads(self.decrypt_text(raw))


def build_envelope_cipher(config: DataProtectionConfig) -> EnvelopeCipher | None:
    if not config.encrypt_at_rest:
        return None
    provider = build_master_key_provider(config)
    return EnvelopeCipher(provider)
