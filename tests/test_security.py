"""Tests for ``core.security`` (cursor signing + metrics token)."""
from __future__ import annotations

import os
import time

import pytest

from core.security import (
    CursorError,
    CursorSigner,
    get_cursor_signer,
    reset_cursor_signer,
    verify_metrics_token,
)


# ── CursorSigner ─────────────────────────────────────────────────


class TestCursorSigner:
    def test_round_trip(self) -> None:
        s = CursorSigner(b"key" * 8)
        token = s.sign("instagram", "alice", "raw_xyz")
        assert s.verify(token, "instagram", "alice") == "raw_xyz"

    def test_empty_cursor_passes_through(self) -> None:
        s = CursorSigner(b"key" * 8)
        assert s.sign("instagram", "alice", "") == ""
        assert s.verify("", "instagram", "alice") == ""

    def test_tamper_rejected(self) -> None:
        s = CursorSigner(b"key" * 8)
        token = s.sign("instagram", "alice", "raw")
        with pytest.raises(CursorError):
            s.verify(token + "x", "instagram", "alice")

    def test_cross_platform_rejected(self) -> None:
        s = CursorSigner(b"key" * 8)
        token = s.sign("instagram", "alice", "raw")
        with pytest.raises(CursorError, match="platform"):
            s.verify(token, "tiktok", "alice")

    def test_cross_username_rejected(self) -> None:
        s = CursorSigner(b"key" * 8)
        token = s.sign("instagram", "alice", "raw")
        with pytest.raises(CursorError, match="username"):
            s.verify(token, "instagram", "bob")

    def test_expired_rejected(self, monkeypatch) -> None:
        s = CursorSigner(b"key" * 8, ttl_seconds=1)
        token = s.sign("instagram", "alice", "raw")
        # Jump time forward past the TTL.
        real_time = time.time()
        monkeypatch.setattr(time, "time", lambda: real_time + 5)
        with pytest.raises(CursorError, match="expired"):
            s.verify(token, "instagram", "alice")

    def test_different_key_rejected(self) -> None:
        a = CursorSigner(b"key-a" * 8)
        b = CursorSigner(b"key-b" * 8)
        token = a.sign("instagram", "alice", "raw")
        with pytest.raises(CursorError, match="signature"):
            b.verify(token, "instagram", "alice")

    def test_malformed_token(self) -> None:
        s = CursorSigner(b"key" * 8)
        with pytest.raises(CursorError):
            s.verify("not-a-valid-token", "instagram", "alice")

    def test_oversize_raw_cursor_rejected(self) -> None:
        s = CursorSigner(b"key" * 8)
        with pytest.raises(CursorError, match="exceeds"):
            s.sign("instagram", "alice", "x" * 10_000)

    def test_oversize_token_rejected(self) -> None:
        s = CursorSigner(b"key" * 8)
        with pytest.raises(CursorError, match="maximum length"):
            s.verify("x" * 10_000, "instagram", "alice")

    def test_empty_key_rejected(self) -> None:
        with pytest.raises(ValueError):
            CursorSigner(b"")

    def test_from_env_uses_env_key(self, monkeypatch) -> None:
        monkeypatch.setenv("CURSOR_SIGNING_KEY", "env-supplied-key")
        s = CursorSigner.from_env()
        token = s.sign("instagram", "alice", "raw")
        assert s.verify(token, "instagram", "alice") == "raw"

    def test_from_env_generates_random_key_when_unset(self, monkeypatch) -> None:
        monkeypatch.delenv("CURSOR_SIGNING_KEY", raising=False)
        # Should still produce a working signer; just won't survive restarts.
        s = CursorSigner.from_env()
        token = s.sign("instagram", "alice", "raw")
        assert s.verify(token, "instagram", "alice") == "raw"


# ── singleton ────────────────────────────────────────────────────


def test_get_cursor_signer_is_singleton(cursor_signing_key) -> None:
    a = get_cursor_signer()
    b = get_cursor_signer()
    assert a is b


def test_reset_cursor_signer_drops_cache(cursor_signing_key) -> None:
    a = get_cursor_signer()
    reset_cursor_signer()
    b = get_cursor_signer()
    assert a is not b


# ── verify_metrics_token ─────────────────────────────────────────


class TestMetricsToken:
    def test_denied_when_env_unset(self, monkeypatch) -> None:
        monkeypatch.delenv("METRICS_AUTH_TOKEN", raising=False)
        assert verify_metrics_token("Bearer anything") is False

    def test_denied_when_header_missing(self, monkeypatch) -> None:
        monkeypatch.setenv("METRICS_AUTH_TOKEN", "secret")
        assert verify_metrics_token(None) is False
        assert verify_metrics_token("") is False

    def test_denied_for_wrong_scheme(self, monkeypatch) -> None:
        monkeypatch.setenv("METRICS_AUTH_TOKEN", "secret")
        assert verify_metrics_token("Basic secret") is False

    def test_denied_for_wrong_token(self, monkeypatch) -> None:
        monkeypatch.setenv("METRICS_AUTH_TOKEN", "secret")
        assert verify_metrics_token("Bearer wrong") is False

    def test_accepted_for_correct_token(self, monkeypatch) -> None:
        monkeypatch.setenv("METRICS_AUTH_TOKEN", "secret")
        assert verify_metrics_token("Bearer secret") is True

    def test_case_insensitive_scheme(self, monkeypatch) -> None:
        monkeypatch.setenv("METRICS_AUTH_TOKEN", "secret")
        assert verify_metrics_token("bearer secret") is True
        assert verify_metrics_token("BEARER secret") is True
