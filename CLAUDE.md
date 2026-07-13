# CLAUDE.md

Context for Claude Code (and future contributors) working in this repository.

## What this project is

A scraping-as-a-service backend for social media profiles (Instagram, TikTok, Facebook). Clients submit a username via HTTP, the request is queued, a pool of Playwright-driven workers scrapes the profile using rotating accounts/sessions, and results are cached and returned via polling. Redis is the **only** datastore — there is no SQL/NoSQL database. It simultaneously serves as: cache, job-state store, account-credential pool, session metadata store, circuit-breaker state, dead-letter queue, rate limiter, metrics counters, and the RQ message broker.

## Architecture / request flow

```
POST /scrape
  → per-IP rate limit (atomic Redis INCR+EXPIRE)
  → verify/unwrap signed pagination cursor (if supplied)
  → cache lookup: profile:{platform}:{username}[:cursor:{cursor}]
  → on miss: dispatch_job()
      → circuit breaker check (per platform)
      → dedup claim (SET NX EX on job:active:{platform}:{username})
      → backpressure check (queue length vs QUEUE_MAX_LENGTH)
      → create `pending` job-state record
      → enqueue onto RQ (rolls back dedup/state on enqueue failure)
  → returns queued / processing / cached + job_id

RQ worker → execute_scrape_job()
  → acquire browser slot (global + per-platform concurrency guard)
  → acquire account (Lua-script atomic lock from account_pool)
  → acquire cache stampede lock (or serve another worker's fresh result)
  → dynamically import & run the platform's *Scraper class (Playwright)
  → persist result to cache + job state, record circuit-breaker success
  → on failure: classify exception → retry w/ backoff, invalidate account/session, or DLQ

GET /job/{job_id}   → poll job state from Redis hash
GET /metrics        → bearer-gated, per-platform + global counters
/admin/*            → bearer-gated (separate token): manage accounts, DLQ, cancel jobs
```

## Directory map

| Path | Responsibility |
|---|---|
| `api/main.py` | FastAPI app, lifespan startup checks, `/health` `/readyz` `/scrape` `/job/{id}` `/metrics` |
| `api/admin.py` | `/admin/*` router — accounts, DLQ, job cancel (bearer-token gated) |
| `api/schemas.py` | Pydantic request/response models, username validation |
| `account_pool/manager.py` | `AccountPoolManager` — Redis-backed credential pool, Lua-script atomic locking, cooldowns, invalidation |
| `cache/manager.py` | `CacheManager` — profile cache, stampede lock, invalidation |
| `core/config.py` | Central `Config` class — single source of truth for all env vars |
| `core/crypto.py` | `CredentialCipher` (Fernet) — at-rest encryption for account credentials |
| `core/exceptions.py` | Typed exception hierarchy (`ScraperBaseError` subclasses) driving retry/DLQ classification |
| `core/job_state.py` | Job lifecycle (pending/processing/completed/failed) in Redis, sync+async |
| `core/platform_config.py` | Pydantic-validated loader for `platform_config.yml` |
| `core/platforms.py` | `Platform` enum (instagram/tiktok/facebook) |
| `core/redis.py` | Async/sync Redis connection pools + health probe |
| `core/security.py` | `CursorSigner` (HMAC pagination cursors), metrics/admin bearer-token checks |
| `queues/dispatcher.py` | Async job dispatch: circuit breaker → dedup → backpressure → enqueue |
| `queues/dead_letter.py` | Per-platform bounded dead-letter queue |
| `scrapers/base.py` | `BaseScraper` ABC — browser lifecycle, session reuse, `execute()` template method |
| `scrapers/{instagram,tiktok,facebook}/scraper.py` | Per-platform scraping logic |
| `sessions/manager.py` | `SessionManager` — Playwright `storage_state` on disk + Redis metadata + DOM-based session validation |
| `utils/anti_detection.py` | Human-like delays, randomized viewport/user-agent |
| `utils/circuit_breaker.py` | Sync + async per-platform circuit breaker (Redis ZSET) |
| `utils/concurrency.py` | `BrowserConcurrencyGuard` — global + per-platform browser slot limiting (Lua) |
| `utils/metrics.py` | Redis counters/timing |
| `utils/retry.py` | Exponential backoff decorator + `RetryContext` |
| `workers/executor.py` | `execute_scrape_job` — the core job-execution state machine |
| `workers/resources.py` | Context managers: `browser_slot`, `acquired_account`, `cache_lock`, `JobOutcome` |
| `workers/runner.py` | CLI entry: `python -m workers.runner [--platform X]`, RQ `SimpleWorker` + graceful shutdown |
| `workers/{instagram,tiktok,facebook}_worker.py` | Deprecated compat shims delegating to `executor.py` |
| `tests/` | pytest suite (17 files + `conftest.py`), `fakeredis`-based, no live Redis needed |
| `platform_config.yml` | Per-platform workers/accounts/ips/browser limits/queue names |

## Config

`core/config.py` is the single source of truth for environment variables; `.env.example` documents all of them. `platform_config.yml` defines per-platform `workers`/`accounts`/`ips`/`max_concurrent_browsers`/`queue_name`, validated at startup (sum of per-platform browser limits ≤ global `MAX_CONCURRENT_BROWSERS`, unique queue names, no unknown platforms — fail-fast).

Security-critical vars that **must** be set in production (all fail closed / insecure if unset):
- `CREDENTIAL_ENCRYPTION_KEY` / `CREDENTIAL_ENCRYPTION_KEYS` — Fernet key(s) encrypting stored account credentials
- `CURSOR_SIGNING_KEY` — HMAC key signing pagination cursors (random per-process key generated with a CRITICAL log if unset)
- `METRICS_AUTH_TOKEN` — bearer token for `/metrics` (denied to all if unset)
- `ADMIN_AUTH_TOKEN` — bearer token for `/admin/*`, intentionally distinct from the metrics token so a leaked metrics token can't mutate accounts/jobs

TTL invariant checked at startup: `CACHE_TTL_SECONDS` should be ≥ `CURSOR_TTL_SECONDS` (else a warning — paginating via cursor can outlive the cache) and `JOB_RESULT_TTL_SECONDS` ≥ `CACHE_TTL_SECONDS`.

## Redis key namespace

| Prefix | Holds |
|---|---|
| `profile:{platform}:{username}[:cursor:{cursor}]` | Cached scrape result (JSON, TTL) |
| `job:{job_id}` | Job state hash (status/result/error/timestamps) |
| `job:active:{platform}:{username}` | Dedup guard |
| `account:{platform}:{account_id}` | Encrypted credentials + status |
| `account_lock:{platform}:{account_id}`, `account_cooldown:{platform}:{account_id}` | Account pool locking/cooldown |
| `accounts:{platform}` | Set of account IDs |
| `session:{platform}:{account_id}` | Session metadata (actual `storage_state` JSON lives on disk under `SESSION_STORAGE_DIR`) |
| `circuit:{platform}:*` | Circuit breaker state/failures/opened_at/probe |
| `active:browsers[:{platform}]` | Browser concurrency counters |
| `dlq:failed_jobs:{platform}`, `metrics:dlq:{platform}:*` | Dead-letter queue + counts |
| `metrics:{platform}:*`, `metrics:timing:{platform}:*` | Operational metrics |
| `ratelimit:{scope}` | Per-IP rate limiting |
| `rq:queue:{queue_name}` | RQ's internal queue keys |

## Running locally

- `docker-compose.yml` — `redis` + `api` + one worker service per platform (`instagram_worker`, `tiktok_worker`, `facebook_worker`), each pinned via `WORKER_PLATFORM`.
- `docker-compose.prod.yml` — adds replica counts (3 IG / 2 TikTok / 2 FB workers), CPU/memory limits, restart policies; Redis runs with AOF persistence and `allkeys-lru` eviction (256MB cap).
- `Dockerfile` — multi-stage build off `mcr.microsoft.com/playwright/python:v1.58.0-noble` (Chromium only), non-root `pwuser`, separate `api`/`worker` targets.

## Testing

```
pytest
```
`pytest.ini`: `testpaths = tests`, `asyncio_mode = auto`, strict markers, `integration` marker for tests needing real external services. `conftest.py` provides `fake_redis`/`fake_async_redis` (via `fakeredis`, no live Redis needed), plus fixtures resetting the crypto/cursor-signing singletons and `platform_config.yml` path between tests. Coverage includes: account pool, admin API, cache invalidation, crypto, DLQ, dedup race conditions, dispatcher, executor, hardening regressions, health endpoints, platform config, rate limiting, resources, retry logic, security, session validation.

## Recent history / trajectory

Only 3 commits so far:
1. `1b4e679` "committing code" — initial prototype scaffold.
2. `520a875` "Add env to gitignore" — added `.env`/`.venv`/`__pycache__` to `.gitignore`.
3. `e561af4` "latest changes added" (2026-07-13 session) — a large hardening pass (4812 insertions / 50 files): added `api/admin.py`, `api/schemas.py`, `core/crypto.py`, `core/platforms.py`, `core/security.py`, the entire `tests/` suite, `workers/resources.py`; reworked account pool locking to be Lua-atomic, added dedup/backpressure/circuit-breaker to the dispatcher, bounded the DLQ, added DOM-based session validation + path-traversal hardening, and added retry classification to the executor.

The project is actively moving from "working prototype" to a hardened, race-condition-aware, security-conscious production service.

## Production-readiness audit (in progress)

Started 2026-07-13. Acting as a Principal Architect / Staff Engineer doing a **module-by-module** production audit before this system carries real traffic. Do not audit everything at once — one module at a time, fix it, re-verify, then move on.

**Methodology per module:** read every file directly (no assuming code is correct because it "looks fine"), then produce: Summary → Findings (severity-ranked: Critical/High/Medium/Low/Suggestion, each with a concrete failure scenario and `file:line`) → Root Cause → Recommended Fix → Confidence verdict (✅ Production Ready / ⚠️ After Minor Fixes / ❌ Not Ready). Apply the fixes, then run the full test suite before moving on.

**Module order:** `core/` → `account_pool/` → `cache/` → `queues/` → `sessions/` → `utils/` → `scrapers/` → `workers/` → `api/`.

### Status
- [x] `core/` — audited and fixed (below)
- [ ] `account_pool/` — **next up**
- [ ] `cache/`
- [ ] `queues/`
- [ ] `sessions/`
- [ ] `utils/`
- [ ] `scrapers/`
- [ ] `workers/`
- [ ] `api/`

### `core/` — findings & resolutions

- **High** — `CURSOR_SIGNING_KEY` silently fell back to a per-process random key with no fail-closed option, breaking cursor verification across multiple API replicas behind a load balancer. **Fixed:** added `Config.ENVIRONMENT`; `api/main.py`'s `lifespan` now refuses to boot when `ENVIRONMENT=production` and `CURSOR_SIGNING_KEY` is unset.
- **Medium** — the dedup guard key (`job:active:{platform}:{username}`, TTL = `JOB_DEDUP_TTL_SECONDS`) could expire mid-flight on a job that retries with backoff, letting a duplicate job dispatch for the same target. **Fixed:** added `JobStateManager.refresh_dedup()` (`core/job_state.py`), called from `workers/executor.py` before every retry backoff.
- **Medium** — `core/redis.py`'s `get_rq_connection()` built a brand-new, unbounded connection pool on every call instead of being a cached singleton like `get_sync_redis`/`get_async_redis`. **Fixed:** made it a cached singleton bounded by `REDIS_MAX_CONNECTIONS`.
- **Medium — investigated, NOT applied.** Routing `verify_metrics_token`/`verify_admin_token` (`core/security.py`) through `Config` instead of live `os.getenv()` looked like a "single source of truth" inconsistency. It isn't: `Config`'s values are frozen at import time, and `tests/test_security.py` + `tests/test_admin.py` rely on `monkeypatch.setenv(...)` taking effect immediately on the next call. Confirmed by running the suite, reverted the change. **This is intentional — do not "fix" it again.**
- **Low** — `AsyncJobStateManager.get_state()` silently swallowed JSON-decode errors on the `result` field. **Fixed:** now logs a warning with `job_id`.
- **Low** — `QueueFullError`/`DuplicateJobError` were each defined twice in `core/exceptions.py` (the second silently shadowing the first). **Fixed:** removed the shadowed pair.
- **Low** — no bounds/positivity validation on numeric `Config` env vars (unlike `platform_config.py`'s Pydantic validation). **Fixed:** added `_int_env`/`_float_env` helpers with `min_value` checks, failing fast at import.
- **Suggestion** — `queues/redis_conn.py` was dead code (confirmed unused via repo-wide grep, only self-referenced). **Fixed:** deleted.

**Verification:** the checked-in `.venv` had x86_64-only `cryptography`/`pydantic-core` wheels under an arm64 interpreter, which blocked the test suite entirely (`ImportError`/PyO3 arch mismatch) — reinstalled matching arm64 wheels to unblock. Then compared the full suite before/after via `git stash`: **15 failures, identical set before and after**, all in `tests/test_account_pool.py` / `tests/test_hardening_fixes.py`, all `redis.exceptions.ResponseError: unknown command 'evalsha'`. This is a **pre-existing `fakeredis` version gap** — it doesn't implement Lua scripting (`EVALSHA`), which `account_pool/manager.py`'s atomic locking relies on. It is **not** a code defect and **not** caused by this audit's changes. When auditing `account_pool/` next, start by checking the `fakeredis` version pin rather than re-diagnosing this from scratch. Everything else passes clean, including `test_security.py` (21/21) and `test_admin.py` (19/19).

## Known quirks / cleanup candidates

- `requirements.txt` lists `Django==4.2.1` and `sqlparse==0.4.4` — unused anywhere in the code (the app is 100% FastAPI); likely vestigial.
- `build_output.log`, `build_output2.log`, and `.DS_Store` are tracked in git despite being build/OS artifacts.
- `.env` was committed in the initial commit before `520a875` added it to `.gitignore` — check whether it's still tracked (`git ls-files | grep '^\.env$'`), and rotate/purge any real secrets from history before pushing to a shared remote.
- `docker-compose.prod.yml`'s Redis uses `allkeys-lru` eviction, which doesn't distinguish disposable cache keys from critical state (account locks, job state) — under memory pressure, eviction could theoretically drop an in-use account lock or job hash.

## Possible next steps

- Clean up the stray tracked files and unused dependencies noted above.
- Add a `README.md` for external/non-Claude readers (this file is Claude-context-focused, not a user-facing intro).
- Confirm `.env` is fully purged from git history before any push to a shared remote.
