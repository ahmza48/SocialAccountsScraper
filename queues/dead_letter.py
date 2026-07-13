"""Dead-letter queue for permanently failed jobs.

Storage model
-------------

* ``dlq:failed_jobs:{platform}`` — the canonical per-platform Redis list.
  Entries are pushed at the head and trimmed to ``Config.DLQ_MAX_LENGTH``
  in the same pipeline so the list cannot grow unbounded.
* ``metrics:dlq:{platform}:total`` — monotonic count of DLQ pushes since the
  Redis instance was created. Lets dashboards detect a sudden spike that
  would otherwise be hidden behind the trim cap.
* ``metrics:dlq:{platform}:dropped`` — incremented when LTRIM evicts an
  entry so operators can tell when the cap is too small.

The previous implementation also kept a global ``dlq:failed_jobs`` list,
which doubled the write amplification and meant the global view drifted
from the per-platform views during partial failures. Aggregation now
happens at read time in :meth:`DeadLetterQueue.list_failed`.
"""
from __future__ import annotations

import json
import time
from typing import Iterable, List, Optional

import redis

from core.config import Config
from core.logging_config import get_logger

logger = get_logger(__name__)

DLQ_KEY_PREFIX = "dlq:failed_jobs"
# Public alias kept for any external callers that imported the legacy name.
DLQ_KEY = DLQ_KEY_PREFIX


def _key(platform: str) -> str:
    return f"{DLQ_KEY_PREFIX}:{platform}"


def _metric_total(platform: str) -> str:
    return f"metrics:dlq:{platform}:total"


def _metric_dropped(platform: str) -> str:
    return f"metrics:dlq:{platform}:dropped"


class DeadLetterQueue:
    """Bounded, observable dead-letter queue."""

    def __init__(self, redis_client: Optional[redis.Redis] = None) -> None:
        if redis_client is not None:
            self._redis = redis_client
        else:
            from core.redis import get_sync_redis
            self._redis = get_sync_redis()

    # ── Write path ───────────────────────────────────────────────

    def push(
        self,
        job_id: str,
        platform: str,
        username: str,
        error: str,
        attempts: int,
    ) -> None:
        """Push a failed job onto the per-platform DLQ atomically.

        Uses a single transactional pipeline so the LPUSH, LTRIM, length
        observation, and total-counter increment either all succeed or all
        fail. The drop counter is incremented only when the trim actually
        evicted something (LPUSH return > cap and post-trim length == cap).
        """
        entry = json.dumps({
            "job_id": job_id,
            "platform": platform,
            "username": username,
            "error": error,
            "attempts": attempts,
            "failed_at": time.time(),
        })

        cap = max(1, Config.DLQ_MAX_LENGTH)
        key = _key(platform)
        pipe = self._redis.pipeline(transaction=True)
        pipe.lpush(key, entry)
        pipe.ltrim(key, 0, cap - 1)
        pipe.llen(key)
        pipe.incr(_metric_total(platform))
        results = pipe.execute()
        new_len_after_push = int(results[0])
        len_after_trim = int(results[2])
        if new_len_after_push > cap and len_after_trim == cap:
            self._redis.incr(_metric_dropped(platform))
            logger.warning(
                "DLQ for %s evicted oldest entry (cap=%d); raise "
                "DLQ_MAX_LENGTH if this is a regular occurrence",
                platform,
                cap,
            )

        logger.error(
            "Job %s moved to DLQ after %d attempts: %s",
            job_id,
            attempts,
            error,
            extra={"job_id": job_id, "platform": platform},
        )

    # ── Read path ────────────────────────────────────────────────

    def list_failed(
        self,
        platform: Optional[str] = None,
        start: int = 0,
        count: int = 50,
    ) -> List[dict]:
        """Return DLQ entries, newest first.

        When ``platform`` is ``None`` the result is an aggregate across every
        platform configured in ``platform_config.yml``. Aggregation reads
        each per-platform list, merges, then sorts by ``failed_at``
        descending so callers get a stable, time-ordered global view.
        """
        if platform is not None:
            return self._read_one(platform, start, count)
        return self._read_all(start, count)

    def _read_one(self, platform: str, start: int, count: int) -> List[dict]:
        end = start + count - 1
        raw = self._redis.lrange(_key(platform), start, end)
        return [parsed for parsed in (_safe_loads(b) for b in raw) if parsed]

    def _read_all(self, start: int, count: int) -> List[dict]:
        merged: List[dict] = []
        # Read enough from each platform to honour the offset after the merge.
        per_platform_window = start + count
        for platform in _registered_platforms():
            merged.extend(self._read_one(platform, 0, per_platform_window))
        merged.sort(key=lambda e: e.get("failed_at", 0.0), reverse=True)
        return merged[start : start + count]

    def length(self, platform: Optional[str] = None) -> int:
        """Return current DLQ length (per platform or summed across all)."""
        if platform is not None:
            return int(self._redis.llen(_key(platform)))
        total = 0
        for p in _registered_platforms():
            total += int(self._redis.llen(_key(p)))
        return total

    def stats(self, platform: str) -> dict:
        """Counters for dashboards: current size + lifetime push + drops."""
        pipe = self._redis.pipeline(transaction=False)
        pipe.llen(_key(platform))
        pipe.get(_metric_total(platform))
        pipe.get(_metric_dropped(platform))
        current, total, dropped = pipe.execute()
        return {
            "platform": platform,
            "current": int(current or 0),
            "total": int(total or 0),
            "dropped": int(dropped or 0),
            "cap": Config.DLQ_MAX_LENGTH,
        }


def _safe_loads(blob) -> Optional[dict]:
    try:
        return json.loads(blob)
    except (TypeError, ValueError) as exc:
        logger.warning("Skipping unparseable DLQ entry: %s", exc)
        return None


def _registered_platforms() -> Iterable[str]:
    """Best-effort enumeration of platforms to aggregate over.

    Reads the platform-config singleton when available; falls back to the
    static enum so a misconfigured config still yields a usable global view.
    """
    try:
        from core.platform_config import get_platform_config

        return list(get_platform_config().platforms)
    except Exception:  # pragma: no cover - exercised when YAML is invalid
        from core.platforms import Platform

        return Platform.values()
