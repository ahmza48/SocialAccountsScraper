"""Shared pytest fixtures.

Uses :mod:`fakeredis` to give every test a clean, in-process Redis without
needing a running server. The fixtures also reset the security-key singletons
between tests so env-var changes are honoured.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Make the project root importable when pytest is run from anywhere.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


@pytest.fixture
def fake_redis():
    """Sync fake Redis client with decode_responses=True (matches prod config)."""
    import fakeredis

    client = fakeredis.FakeRedis(decode_responses=True)
    try:
        yield client
    finally:
        client.flushall()
        client.close()


@pytest.fixture
async def fake_async_redis():
    """Async fake Redis client (decode_responses=True)."""
    import fakeredis.aioredis

    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        yield client
    finally:
        await client.flushall()
        await client.aclose()


@pytest.fixture
def fernet_key(monkeypatch):
    """Generate a fresh Fernet key and expose it via env + reset cipher singleton."""
    from cryptography.fernet import Fernet

    from core.crypto import reset_credential_cipher

    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", key)
    monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEYS", raising=False)
    reset_credential_cipher()
    yield key
    reset_credential_cipher()


@pytest.fixture
def cursor_signing_key(monkeypatch):
    """Set a deterministic cursor signing key and reset the singleton."""
    from core.security import reset_cursor_signer

    key = "test-cursor-signing-key-32-bytes-long!!"
    monkeypatch.setenv("CURSOR_SIGNING_KEY", key)
    reset_cursor_signer()
    yield key
    reset_cursor_signer()


@pytest.fixture
def reset_platform_config(monkeypatch, tmp_path):
    """Point ``PLATFORM_CONFIG_PATH`` at a tmp file and reset the singleton.

    Returns a ``write(yaml_text)`` helper so each test can drop in its own
    config text without touching the real ``platform_config.yml``.
    """
    from core.config import Config
    from core.platform_config import reset_platform_config as _reset

    config_path = tmp_path / "platform_config.yml"
    monkeypatch.setattr(Config, "PLATFORM_CONFIG_PATH", str(config_path))
    _reset()

    def write(yaml_text: str) -> None:
        config_path.write_text(yaml_text)
        _reset()

    yield write
    _reset()
