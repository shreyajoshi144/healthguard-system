> **SUPERSEDED.** This document predates the release audit. Its "0 skipped" claim was not reproducible on a clean checkout (7 tests skipped until the
> test suite was fixed), and "alert delivery not implemented" is no longer true. The authoritative, current evidence is `docs/RELEASE_AUDIT.md`.

# HealthGuard P0 — closure evidence (fresh run)

**Classification: P0 — PARTIALLY CLOSED.** Every in-repository gate below passes with nothing skipped, but the real-provider
paths listed under UNVERIFIED (real email delivery, real AI provider, real Redis/S3, capacity) have not been exercised, so P0 is not marked CLOSED.

## Fresh results (this run; reproduce with the commands shown)
| Gate | Command | Result |
|---|---|---|
| P0 suite | `pytest -q tests/p0` | **124 passed** |
| Full suite | `pytest -q` | **479 passed, 0 skipped, 0 failed** (spaCy `en_core_web_md` installed, so the previously skipped semantic tests ran) |
| Sprint 1–5 regression | `pytest -q tests/sprint1 tests/sprint2 tests/sprint3_test_*.py tests/test_sprint3_* tests/test_sprint4_* tests/test_sprint5_*` | **295 passed** |
| Browser E2E (real Chromium + uvicorn) | `PLAYWRIGHT_BROWSERS_PATH=… pytest tests/p0/test_browser.py tests/sprint1/test_browser_e2e.py` | **2 passed** |
| compileall | `python -m compileall -q .` | OK |
| Frontend | `node --check frontend/js/*.js` (17 files) + `tests/p0/test_frontend_contract.py` (assets resolve, every `/api` path used by the P0 pages matches a backend route) | OK / 6 passed. No JS build/type/lint toolchain exists in this repo. |
| SQLite migrations | `alembic upgrade head` → `alembic check` | head `0016_email_reminders`; *No new upgrade operations detected* |
| PostgreSQL migrations (real Postgres) | `alembic upgrade head`, `scripts/check-schema.py`, `alembic check`, `downgrade base` → `upgrade head` → `check` | head `0016_email_reminders`; *No new upgrade operations detected* (before and after the round trip) |
| Real-PostgreSQL tests | `tests/p0/test_postgres_migrations.py` (needs `pgserver`, see `requirements-dev.txt`) | 3 passed: roundtrip + zero drift, data preserved across enum→varchar, DB-level reminder idempotency + 3 concurrent workers never claim a row twice |

## What changed in this closure pass
* Removed Sprint 6 naming (docs, test names, migration revision `0012_p0_measurements`); vitals and all migrations preserved. P0 tests now live in `tests/p0/`.
* Fixed PostgreSQL schema drift with migration `0015_schema_alignment` (6 native ENUM columns → VARCHAR with values preserved; redundant unique constraint dropped; SQLite provenance columns widened). `alembic check` is clean on both databases.
* Fixed an order-dependent test failure that also occurs on the original Sprint 5 ZIP (shared in-process rate limiter) via `tests/conftest.py`; no assertion was changed.
* Removed runtime artifacts (`__pycache__`, `.pytest_cache`, generated `data/splits`, databases); `logs/app.log` restored to its original content.

## New: email reminders for confirmed/scheduled follow-ups (email only)
Implemented: per-patient opt-in preferences (lead times, time of day, time zone, optional title), idempotent planning, DB-backed delivery ledger, dispatch from the existing worker loop over the existing SMTP configuration, exponential backoff (60s doubling, 1h cap, 5 attempts), permanent-failure short-circuit, expiry of late reminders (>12h), lease recovery, cancellation on completion/cancel/reschedule/disable, send-time authorization re-check, delivery status UI, audit events, admin counts + alerts. Emails carry no health details unless the user opts in to the title. Details: `deploy/PRODUCTION.md`.
Verified by `tests/p0/test_reminders.py` (30 tests, incl. a protocol-level SMTP capture server for success/transient/permanent failures, multi-worker claim exclusivity on SQLite and PostgreSQL, revocation/expiry/scope-removal/deactivation/patient-deletion cancellation, DST scheduling, IDOR, PHI minimisation, audit) and the browser journey (preferences → scheduled → cancelled).

## UNVERIFIED (not tested here; do not claim)
* **Real email delivery** — tests use a local SMTP protocol server, not a mail provider; deliverability, SPF/DKIM, bounce handling untested. Delivery is at-least-once (a crash between SMTP acceptance and the status write can re-send once; `Message-ID` is deterministic).
* Real external AI provider (Ask degrades to an error state without one); real multi-worker Redis for document jobs; real S3; real operational alert delivery (not implemented — `NOT_CONFIGURED`); 200-user capacity; PostgreSQL connection-pool exhaustion; load/SLO, backup/restore, rollback and dependency-failure drills; HIPAA/GDPR/clinical validation (no claim made).

## Known gaps
* Admin AI metrics are per-process counters; no token/cost data.
* Legacy survey analytics (`/api/upload`, `/api/metrics`) still keeps state in process memory (breaks horizontal scaling).
* No automated accessibility audit; responsive coverage is a single 390px overflow check.
* The reminder window between claim and send is small but non-zero: an action completed in that instant is re-validated just before sending, not after.
* **Exact original dog asset: unavailable.** It is not in the supplied ZIP; nothing was generated or substituted.
