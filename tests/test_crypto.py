"""Tests for ``core.crypto`` (Fernet credential encryption)."""
from __future__ import annotations

import pytest

from cryptography.fernet import Fernet

from core.crypto import (
    CredentialCipher,
    CredentialCipherError,
    get_credential_cipher,
    reset_credential_cipher,
)


@pytest.fixture
def cipher(fernet_key) -> CredentialCipher:
    return CredentialCipher.from_env()


class TestEncryptDecrypt:
    def test_round_trip(self, cipher: CredentialCipher) -> None:
        creds = {"username": "alice", "password": "s3cret!"}
        token = cipher.encrypt(creds)
        assert token.startswith("ENC:v1:")
        assert cipher.decrypt(token) == creds

    def test_decrypt_strips_prefix(self, cipher: CredentialCipher) -> None:
        token = cipher.encrypt({"a": 1})
        # Decrypt should accept either prefixed or bare token form.
        bare = token[len("ENC:v1:"):]
        assert cipher.decrypt(bare) == {"a": 1}

    def test_encrypt_rejects_non_dict(self, cipher: CredentialCipher) -> None:
        with pytest.raises(CredentialCipherError):
            cipher.encrypt("just a string")  # type: ignore[arg-type]

    def test_decrypt_rejects_empty(self, cipher: CredentialCipher) -> None:
        with pytest.raises(CredentialCipherError):
            cipher.decrypt("")

    def test_decrypt_rejects_tampered_token(self, cipher: CredentialCipher) -> None:
        token = cipher.encrypt({"a": 1})
        tampered = token[:-2] + "XX"
        with pytest.raises(CredentialCipherError):
            cipher.decrypt(tampered)

    def test_is_encrypted_helper(self, cipher: CredentialCipher) -> None:
        token = cipher.encrypt({"a": 1})
        assert CredentialCipher.is_encrypted(token)
        assert not CredentialCipher.is_encrypted("plaintext")
        assert not CredentialCipher.is_encrypted(None)  # type: ignore[arg-type]
        assert not CredentialCipher.is_encrypted(42)  # type: ignore[arg-type]


class TestRotation:
    def test_rotate_re_encrypts_with_current_key(self, monkeypatch) -> None:
        old_key = Fernet.generate_key().decode("ascii")
        new_key = Fernet.generate_key().decode("ascii")

        # Encrypt with old-only.
        old_only = CredentialCipher([old_key.encode("ascii")])
        token_old = old_only.encrypt({"a": 1})

        # Rotate: new key first, old key still trusted for decryption.
        rotating = CredentialCipher(
            [new_key.encode("ascii"), old_key.encode("ascii")]
        )
        # Old token must still decrypt.
        assert rotating.decrypt(token_old) == {"a": 1}

        # Rotating produces a new token decryptable by the new-only cipher.
        new_token = rotating.rotate(token_old)
        new_only = CredentialCipher([new_key.encode("ascii")])
        assert new_only.decrypt(new_token) == {"a": 1}

    def test_from_env_prefers_keys_over_key(self, monkeypatch) -> None:
        a = Fernet.generate_key().decode("ascii")
        b = Fernet.generate_key().decode("ascii")
        monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", a)
        monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEYS", f"{b},{a}")
        cipher = CredentialCipher.from_env()
        # New tokens encrypt with the first key in the rotation list (b).
        b_only = CredentialCipher([b.encode("ascii")])
        token = cipher.encrypt({"a": 1})
        assert b_only.decrypt(token) == {"a": 1}


class TestEnvLoading:
    def test_missing_env_raises(self, monkeypatch) -> None:
        monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY", raising=False)
        monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEYS", raising=False)
        with pytest.raises(CredentialCipherError, match="CREDENTIAL_ENCRYPTION_KEY"):
            CredentialCipher.from_env()

    def test_singleton_is_cached(self, fernet_key) -> None:
        a = get_credential_cipher()
        b = get_credential_cipher()
        assert a is b

    def test_reset_drops_cache(self, fernet_key) -> None:
        a = get_credential_cipher()
        reset_credential_cipher()
        b = get_credential_cipher()
        assert a is not b
