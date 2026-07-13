"""Tests for ``utils.retry``."""
from __future__ import annotations

import pytest

from core.exceptions import ParsingError, ScrapingError
from utils.retry import RetryContext, exponential_backoff


class TestRetryContext:
    def test_should_not_retry_parsing_error(self) -> None:
        ctx = RetryContext(max_retries=3)
        assert not ctx.should_retry(ParsingError("nope"))

    def test_should_retry_until_max(self) -> None:
        ctx = RetryContext(max_retries=2)
        assert ctx.should_retry(ScrapingError("x"))  # attempt 1
        assert ctx.should_retry(ScrapingError("x"))  # attempt 2
        assert not ctx.should_retry(ScrapingError("x"))  # attempt 3 → exhausted

    def test_wait_calls_sleep_once(self, monkeypatch) -> None:
        calls: list = []
        monkeypatch.setattr("utils.retry.time.sleep", lambda d: calls.append(d))
        ctx = RetryContext(max_retries=3, base_delay=0.5)
        ctx.should_retry(ScrapingError("x"))  # advances attempt to 1
        ctx.wait()
        # F1 regression: ensure we're not double-sleeping.
        assert len(calls) == 1
        assert calls[0] >= 0.5  # base_delay + jitter


class TestExponentialBackoffDecorator:
    def test_returns_on_success(self, monkeypatch) -> None:
        monkeypatch.setattr("utils.retry.time.sleep", lambda d: None)

        @exponential_backoff(max_retries=2, base_delay=0.01)
        def f() -> str:
            return "ok"

        assert f() == "ok"

    def test_retries_on_failure_then_succeeds(self, monkeypatch) -> None:
        monkeypatch.setattr("utils.retry.time.sleep", lambda d: None)
        attempts = {"n": 0}

        @exponential_backoff(max_retries=3, base_delay=0.01)
        def f() -> str:
            attempts["n"] += 1
            if attempts["n"] < 2:
                raise ScrapingError("transient")
            return "ok"

        assert f() == "ok"
        assert attempts["n"] == 2

    def test_non_retryable_raises_immediately(self, monkeypatch) -> None:
        monkeypatch.setattr("utils.retry.time.sleep", lambda d: None)
        attempts = {"n": 0}

        @exponential_backoff(max_retries=3, base_delay=0.01)
        def f() -> None:
            attempts["n"] += 1
            raise ParsingError("bad")

        with pytest.raises(ParsingError):
            f()
        assert attempts["n"] == 1

    def test_exhausts_and_raises_last(self, monkeypatch) -> None:
        monkeypatch.setattr("utils.retry.time.sleep", lambda d: None)

        @exponential_backoff(max_retries=2, base_delay=0.01)
        def f() -> None:
            raise ScrapingError("permanent")

        with pytest.raises(ScrapingError, match="permanent"):
            f()
