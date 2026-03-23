from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

try:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
except ModuleNotFoundError:  # pragma: no cover
    InvalidSignature = Exception  # type: ignore[assignment]
    serialization = None  # type: ignore[assignment]
    Ed25519PrivateKey = None  # type: ignore[assignment]
    Ed25519PublicKey = None  # type: ignore[assignment]


def compute_skill_checksum(path: str) -> str:
    target = Path(path).expanduser()
    digest = hashlib.sha256()
    if target.is_file():
        _update_digest_for_file(digest, target, "__single_file__")
    elif target.is_dir():
        files = sorted(p for p in target.rglob("*") if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts)
        for file_path in files:
            rel = str(file_path.relative_to(target)).replace("\\", "/")
            _update_digest_for_file(digest, file_path, rel)
    else:
        raise ValueError(f"Skill path not found: {path}")
    return f"sha256:{digest.hexdigest()}"


def default_signature_path(path: str) -> Path:
    target = Path(path).expanduser()
    if target.is_dir():
        return target / "skill.sig.json"
    return Path(str(target) + ".sig.json")


def generate_skill_keypair(
    *,
    private_key_path: str | None = None,
    public_key_path: str | None = None,
    overwrite: bool = False,
) -> tuple[Path, Path]:
    _require_crypto()
    private_path = Path(private_key_path).expanduser() if private_key_path else _default_private_key_path()
    public_path = Path(public_key_path).expanduser() if public_key_path else _default_public_key_path(private_path)
    if not overwrite and (private_path.exists() or public_path.exists()):
        raise ValueError("Key path already exists; pass overwrite=True or choose different paths")
    private_path.parent.mkdir(parents=True, exist_ok=True)
    public_path.parent.mkdir(parents=True, exist_ok=True)

    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    private_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    public_path.write_bytes(
        public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    try:
        os.chmod(private_path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return private_path, public_path


def sign_skill(path: str, *, private_key_path: str, signature_path: str | None = None, signer: str | None = None) -> Path:
    _require_crypto()
    skill_checksum = compute_skill_checksum(path)
    private_key = _load_private_key(private_key_path)
    payload = {"checksum": skill_checksum}
    message = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = private_key.sign(message)
    public_key = private_key.public_key()
    fingerprint = _public_key_fingerprint(public_key)
    out_path = Path(signature_path).expanduser() if signature_path else default_signature_path(path)
    doc = {
        "algorithm": "ed25519",
        "checksum": skill_checksum,
        "signature_b64": base64.b64encode(signature).decode("ascii"),
        "signed_at": datetime.now(timezone.utc).isoformat(),
        "signer": signer or "",
        "public_key_fingerprint": fingerprint,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8")
    return out_path


def verify_skill_signature(
    path: str,
    *,
    public_key_paths: list[str],
    signature_path: str | None = None,
) -> tuple[bool, str]:
    _require_crypto()
    sig_path = Path(signature_path).expanduser() if signature_path else default_signature_path(path)
    if not sig_path.exists():
        return False, f"Signature file not found: {sig_path}"
    signature_doc = json.loads(sig_path.read_text(encoding="utf-8"))
    if not isinstance(signature_doc, dict):
        return False, "Invalid signature file format"
    checksum = str(signature_doc.get("checksum", ""))
    signature_b64 = str(signature_doc.get("signature_b64", ""))
    if not checksum or not signature_b64:
        return False, "Signature file missing checksum or signature_b64"
    actual_checksum = compute_skill_checksum(path)
    if checksum != actual_checksum:
        return False, f"Checksum mismatch: expected {checksum}, got {actual_checksum}"
    try:
        signature = base64.b64decode(signature_b64.encode("ascii"))
    except Exception:
        return False, "Invalid base64 signature"

    payload = {"checksum": checksum}
    message = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if not public_key_paths:
        return False, "No public keys provided for verification"
    for key_path in public_key_paths:
        key = _load_public_key(key_path)
        try:
            key.verify(signature, message)
            return True, f"Verified with {key_path}"
        except InvalidSignature:
            continue
    return False, "Signature did not verify against provided public keys"


def _update_digest_for_file(digest: Any, file_path: Path, rel_path: str) -> None:
    digest.update(rel_path.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(file_path.read_bytes())
    digest.update(b"\x00")


def _load_private_key(path: str) -> Ed25519PrivateKey:
    raw = Path(path).expanduser().read_bytes()
    key = serialization.load_pem_private_key(raw, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("Private key must be an Ed25519 PEM key")
    return key


def _load_public_key(path: str) -> Ed25519PublicKey:
    raw = Path(path).expanduser().read_bytes()
    key = serialization.load_pem_public_key(raw)
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("Public key must be an Ed25519 PEM key")
    return key


def _public_key_fingerprint(key: Ed25519PublicKey) -> str:
    key_bytes = key.public_bytes(encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
    return hashlib.sha256(key_bytes).hexdigest()[:16]


def _require_crypto() -> None:
    if serialization is None or Ed25519PrivateKey is None or Ed25519PublicKey is None:
        raise RuntimeError("cryptography package with Ed25519 support is required for skill signing")


def _default_private_key_path() -> Path:
    return _state_dir() / "keys" / "skills_ed25519.pem"


def _default_public_key_path(private_path: Path) -> Path:
    return private_path.with_name(private_path.stem + ".pub.pem")


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
