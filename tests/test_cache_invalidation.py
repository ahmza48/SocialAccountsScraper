"""Tests for cache invalidation and the cache-hit metric correctness fix."""
from __future__ import annotations

import pytest

from cache.manager import CacheManager


@pytest.fixture
def cache(fake_redis):
    return CacheManager(redis_client=fake_redis)


def test_invalidate_does_not_overmatch_username_prefix(cache, fake_redis):
    """`invalidate("alice")` MUST NOT drop entries for `alice2`."""
    cache.set_profile("instagram", "alice", {"name": "alice"})
    cache.set_profile("instagram", "alice2", {"name": "alice2"})
    cache.set_page("instagram", "alice", "next-cursor", {"items": []})

    cache.invalidate("instagram", "alice")

    # alice's profile + cursor entries gone …
    assert cache.get_profile("instagram", "alice") is None
    assert cache.get_page("instagram", "alice", "next-cursor") is None
    # … but alice2 untouched.
    assert cache.get_profile("instagram", "alice2") == {"name": "alice2"}


def test_invalidate_clears_multiple_cursor_pages(cache, fake_redis):
    cache.set_profile("instagram", "bob", {"name": "bob"})
    for cur in ("c1", "c2", "c3"):
        cache.set_page("instagram", "bob", cur, {"cursor": cur})

    cache.invalidate("instagram", "bob")

    assert cache.get_profile("instagram", "bob") is None
    for cur in ("c1", "c2", "c3"):
        assert cache.get_page("instagram", "bob", cur) is None


def test_invalidate_is_safe_when_nothing_cached(cache):
    # Should not raise even if no entries match.
    cache.invalidate("instagram", "ghost")
