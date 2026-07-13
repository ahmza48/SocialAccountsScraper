"""Symmetric encryption for at-rest secrets (account credentials, etc.).

Backed by ``cryptography.fernet`` \u2014 AES-128-CBC + HMAC-SHA256 with timestamps,
managed by ``MultiFernet`` so operators can rotate keys without downtime.

Wire format inside Redis: a Fernet token prefixed with ``ENC:v1:`` so we can
distinguish encrypted blobs from any legacy plaintext that may still exist
during a migration window. Production deployments should refuse to start
without a key configured.
"""
from __future__ import annotations

import json
import os
from typing import Iterable, List, Optional

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from core.logging_config import get_logger

logger = get_logger(__name__)

_TOKEN_PREFIX = "ENC:v1:"


class CredentialCipherError(RuntimeError):
    """Raised when encryption configuration is missing or a token is invalid."""


class CredentialCipher:
    """Encrypt/decrypt JSON-serialisable credential dicts.

    Supports key rotation: the *first* key in ``keys`` is used to encrypt,
    and any key in the list can decrypt. Rotate by prepending a new key,
    re-encrypting old records lazily, and then dropping the trailing key.
    """

    def __init__(self, keys: Iterable[bytes]) -> None:
        key_list: List[bytes] = [k for k in keys if k]
        if not key_list:
            raise CredentialCipherError(
                "CredentialCipher requires at least one Fernet key"
            )
        self._fernet = MultiFernet([Fernet(k) for k in key_list])

    @classmethod
    def from_env(
        cls,
        primary_var: str = "CREDENTIAL_ENCRYPTION_KEY",
        rotation_var: str = "CREDENTIAL_ENCRYPTION_KEYS",
    ) -> "CredentialCipher":
        """Build a cipher from env vars.

        ``CREDENTIAL_ENCRYPTION_KEY`` (single key, the common case) takes
        precedence; ``CREDENTIAL_ENCRYPTION_KEYS`` (comma-separated, newest
        first) is used during rotation.

        Raises :class:`CredentialCipherError` if neither is set so a
        misconfigured deployment fails loudly at startup rather than
        silently storing plaintext.
        """
        rotation_raw = os.getenv(rotation_var, "").strip()
        if rotation_raw:
            keys = [k.strip().encode("ascii") for k in rotation_raw.split(",") if k.strip()]
        else:
            primary = os.getenv(primary_var, "").strip()
            if not primary:
                raise CredentialCipherError(
                    f"Set {primary_var} (or {rotation_var}) to a Fernet key. "
                    f"Generate one with: "
                    f'python -c "from cryptography.fernet import Fernet; '
                    f'print(Fernet.generate_key().decode())"'
                )
            keys = [primary.encode("ascii")]
        return cls(keys)

    # ── Encrypt / Decrypt ────────────────────────────────────────

    def encrypt(self, credentials: dict) -> str:
        """Return a Fernet token (prefixed) for the given credentials dict."""
        if not isinstance(credentials, dict):
            raise CredentialCipherError(
                f"credentials must be a dict, got {type(credentials).__name__}"
            )
        plaintext = json.dumps(credentials, separators=(",", ":")).encode("utf-8")
        token = self._fernet.encrypt(plaintext).decode("ascii")
        return f"{_TOKEN_PREFIX}{token}"

    def decrypt(self, blob: str) -> dict:
        """Decrypt a Fernet token (with or without the prefix) back to a dict.

        Raises :class:`CredentialCipherError` on any failure.
        """
        if not isinstance(blob, str) or not blob:
            raise CredentialCipherError("cannot decrypt empty value")
        token = blob[len(_TOKEN_PREFIX):] if blob.startswith(_TOKEN_PREFIX) else blob
        try:
            plaintext = self._fernet.decrypt(token.encode("ascii"))
        except InvalidToken as exc:
            raise CredentialCipherError("credential token invalid or tampered") from exc
        try:
            return json.loads(plaintext.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise CredentialCipherError("decrypted payload is not valid JSON") from exc

    def rotate(self, blob: str) -> str:
        """Re-encrypt ``blob`` with the current primary key.

        Useful for a background sweep that lazily upgrades records to a newly
        prepended key after rotation. Tolerates already-current tokens.
        """
        decrypted = self.decrypt(blob)
        return self.encrypt(decrypted)

    @staticmethod
    def is_encrypted(blob: object) -> bool:
        """Return True if ``blob`` looks like a CredentialCipher token."""
        return isinstance(blob, str) and blob.startswith(_TOKEN_PREFIX)


_cipher: Optional[CredentialCipher] = None


def get_credential_cipher() -> CredentialCipher:
    """Lazy singleton for :class:`CredentialCipher`."""
    global _cipher
    if _cipher is None:
        _cipher = CredentialCipher.from_env()
    return _cipher


def reset_credential_cipher() -> None:
    """Drop the cached cipher (used by tests after env mutation)."""
    global _cipher
    _cipher = None
