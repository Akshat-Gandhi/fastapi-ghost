"""Password hashing, session tokens, and encryption of stored auth headers."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet

_N, _R, _P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_hex, digest_hex = stored.split("$")
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=_N, r=_R, p=_P)
    except ValueError:
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


def new_session_token() -> tuple[str, str]:
    """Return (token for the cookie, hash to store)."""
    token = secrets.token_urlsafe(32)
    return token, hash_token(token)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _fernet() -> Fernet:
    key = os.environ.get("GHOST_SECRET_KEY")
    if not key:
        key_file = Path(os.environ.get("GHOST_DATA_DIR", ".")) / "secret.key"
        if not key_file.exists():
            key_file.parent.mkdir(parents=True, exist_ok=True)
            key_file.write_bytes(Fernet.generate_key())
            key_file.chmod(0o600)
        key = key_file.read_text().strip()
    return Fernet(key.encode())


def encrypt_headers(headers: dict[str, str]) -> str:
    return _fernet().encrypt(json.dumps(headers).encode()).decode() if headers else ""


def decrypt_headers(blob: str) -> dict[str, str]:
    return json.loads(_fernet().decrypt(blob.encode())) if blob else {}


def parse_headers(text: str) -> dict[str, str]:
    """Parse 'Name: value' lines."""
    headers: dict[str, str] = {}
    for line in text.splitlines():
        if ":" in line:
            name, value = line.split(":", 1)
            if name.strip():
                headers[name.strip()] = value.strip()
    return headers
