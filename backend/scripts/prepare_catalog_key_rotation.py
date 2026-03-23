#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from orchestrator.skills.signing import generate_skill_keypair


def _load_keyring(path: Path) -> dict:
    if not path.exists():
        return {"public_keys": []}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"public_keys": []}
    if not isinstance(raw, dict):
        return {"public_keys": []}
    keys = raw.get("public_keys", [])
    if not isinstance(keys, list):
        keys = []
    return {"public_keys": [str(item).strip() for item in keys if str(item).strip()]}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prepare next MMY catalog signing keypair (rotation prep).",
    )
    parser.add_argument(
        "--name",
        default="v2",
        help="Short key suffix (default: v2). Produces catalog_ed25519_<name>.pub.pem",
    )
    parser.add_argument(
        "--private-out",
        required=True,
        help="Path for private key output (local/offline secret storage).",
    )
    parser.add_argument(
        "--register-public-key",
        action="store_true",
        help="Also append new public key filename to catalog_trusted_keys.json.",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    keys_dir = root / "orchestrator" / "skills" / "keys"
    keys_dir.mkdir(parents=True, exist_ok=True)

    safe_name = str(args.name).strip().lower().replace(" ", "_")
    if not safe_name:
        safe_name = "v2"
    pub_name = f"catalog_ed25519_{safe_name}.pub.pem"
    pub_path = keys_dir / pub_name
    private_path = Path(args.private_out).expanduser().resolve()

    generate_skill_keypair(
        private_key_path=str(private_path),
        public_key_path=str(pub_path),
        overwrite=False,
    )

    if args.register_public_key:
        keyring_path = keys_dir / "catalog_trusted_keys.json"
        keyring = _load_keyring(keyring_path)
        current = keyring.get("public_keys", [])
        if pub_name not in current:
            current.append(pub_name)
        keyring["public_keys"] = current
        keyring_path.write_text(json.dumps(keyring, indent=2, sort_keys=True), encoding="utf-8")

    print(f"Generated catalog keypair.")
    print(f"Private key: {private_path}")
    print(f"Public key:  {pub_path}")
    print(f"Registered in keyring: {'yes' if args.register_public_key else 'no'}")
    print("")
    print("Next steps:")
    print("1) Keep private key offline/secret. Do not commit it.")
    print("2) Re-sign catalog_manifest.json with the new private key in release workflow.")
    print("3) Once validated, retire old key from catalog_trusted_keys.json.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
