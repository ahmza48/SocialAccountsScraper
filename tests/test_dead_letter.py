"""Tests for the bounded DLQ writer."""
from __future__ import annotations

import pytest

from core.config import Config
from queues.dead_letter import DeadLetterQueue, _key, _metric_dropped, _metric_total


@pytest.fixture
def dlq(fake_redis):
    return DeadLetterQueue(redis_client=fake_redis)


def test_push_writes_only_per_platform_list(dlq, fake_redis):
    dlq.push("j1", "instagram", "u", "boom", attempts=3)
    assert fake_redis.llen(_key("instagram")) == 1
    # Legacy global key is no longer dual-written.
    assert fake_redis.llen("dlq:failed_jobs") == 0


def test_push_increments_total_counter(dlq, fake_redis):
    for i in range(3):
        dlq.push(f"j{i}", "instagram", "u", "boom", attempts=1)
    assert int(fake_redis.get(_metric_total("instagram"))) == 3


def test_push_trims_to_cap_and_records_drop(dlq, fake_redis, monkeypatch):
    monkeypatch.setattr(Config, "DLQ_MAX_LENGTH", 3)
    for i in range(5):
        dlq.push(f"j{i}", "instagram", "u", "boom", attempts=1)
    assert fake_redis.llen(_key("instagram")) == 3
    # Two evictions: pushes 4 and 5 each kicked the oldest entry out.
    assert int(fake_redis.get(_metric_dropped("instagram"))) == 2


def test_list_failed_per_platform_returns_newest_first(dlq):
    for i in range(3):
        dlq.push(f"j{i}", "instagram", "u", "boom", attempts=1)
    entries = dlq.list_failed("instagram")
    assert [e["job_id"] for e in entries] == ["j2", "j1", "j0"]


def test_list_failed_aggregates_across_platforms(dlq, monkeypatch, reset_platform_config):
    reset_platform_config("""
version: 1
platforms:
  instagram:
    workers: 1
    accounts: 1
    ips: 1
    max_concurrent_browsers: 1
    queue_name: scrape_instagram
  tiktok:
    workers: 1
    accounts: 1
    ips: 1
    max_concurrent_browsers: 1
    queue_name: scrape_tiktok
""")
    dlq.push("ig-1", "instagram", "u", "e", attempts=1)
    dlq.push("tt-1", "tiktok", "u", "e", attempts=1)
    dlq.push("ig-2", "instagram", "u", "e", attempts=1)

    aggregated = dlq.list_failed(platform=None, count=10)
    job_ids = [e["job_id"] for e in aggregated]
    assert set(job_ids) == {"ig-1", "ig-2", "tt-1"}
    # Sorted newest-first by failed_at — ig-2 was the last push.
    assert job_ids[0] == "ig-2"


def test_length_global_sums_per_platform(dlq, reset_platform_config):
    reset_platform_config("""
version: 1
platforms:
  instagram:
    workers: 1
    accounts: 1
    ips: 1
    max_concurrent_browsers: 1
    queue_name: scrape_instagram
  tiktok:
    workers: 1
    accounts: 1
    ips: 1
    max_concurrent_browsers: 1
    queue_name: scrape_tiktok
""")
    dlq.push("ig-1", "instagram", "u", "e", attempts=1)
    dlq.push("tt-1", "tiktok", "u", "e", attempts=1)
    dlq.push("tt-2", "tiktok", "u", "e", attempts=1)
    assert dlq.length() == 3
    assert dlq.length("tiktok") == 2


def test_stats_reports_current_total_dropped(dlq, fake_redis, monkeypatch):
    monkeypatch.setattr(Config, "DLQ_MAX_LENGTH", 2)
    for i in range(4):
        dlq.push(f"j{i}", "instagram", "u", "e", attempts=1)
    stats = dlq.stats("instagram")
    assert stats["current"] == 2
    assert stats["total"] == 4
    assert stats["dropped"] == 2
    assert stats["cap"] == 2
