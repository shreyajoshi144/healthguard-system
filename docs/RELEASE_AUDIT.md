# HealthGuard — authoritative release evidence report

## VERDICT: NOT LAUNCH-READY

**Companion documents:** [`../README.md`](../README.md) (product overview), [`ARCHITECTURE.md`](ARCHITECTURE.md), [`DEPLOYMENT.md`](DEPLOYMENT.md), [`OPERATIONS.md`](OPERATIONS.md), [`SPRINTS.md`](SPRINTS.md), [`EVALUATION.md`](EVALUATION.md).

> **Correction in this build:** an earlier copy of this report said the baseline failed 12 of 17 gates. The machine-written report `evaluation/healthguard/reports/baseline_before_fixes.json` records 10 of 17, and that is what the README, `EVALUATION.md` and `SPRINTS.md` already said. This report now agrees with the data.

Launch is blocked until the items under "Open blockers" are closed. Nothing here is a clinical-validation, regulatory-compliance, accuracy or capacity claim.

## Open blockers (exact)

| # | Blocker | Evidence |
|---|---|---|
| B1 | **199 known vulnerabilities in 9 pinned dependencies** (PyJWT 2.7.0, starlette 0.41.3, python-multipart 0.0.20, pillow 11.3.0, pdfminer-six, lxml, lxml-html-clean, nltk, streamlit). | `docs/evidence_pip_audit_current_pins.txt` (executed). A candidate upgrade set audits clean (`docs/evidence_dependency_upgrade_candidate_UNVERIFIED.txt`) but was **not adopted**: it moves to starlette 1.x / fastapi 0.162 and the test run on it was aborted at the first failure (`test_admin_surface_has_no_patient_routes...`, 14 passed before it). The full effect is unknown. Pins in `requirements.txt` are unchanged. |
| B2 | **Python 3.11 / Docker image never verified.** Dockerfile builds on 3.11; the full suite ran only on 3.12. Docker is unavailable here. | 3.11.17 environment created and dependencies imported; **the suite was not run on it**. |
| B3 | **No real-infrastructure verification** of S3, a real AI provider, alert delivery to a real receiver, backups/restore, rollback, load/SLO, DB-pool exhaustion, multi-worker/horizontal scaling. | Not executed (see "Verification classes"). |
| B4 | Safety classifier generalization: on fresh wording it missed 4 of 12 diagnosis/treatment requests (recall 67%) before general fixes. It is pattern-based; its post-fix numbers are contaminated (see AI evaluation). It is one layer; unsafe *outputs* are blocked independently (gate `safety.unsafe_output_block_rate` = 1.0 on 12 cases). | `evaluation/healthguard/reports/holdout_first_run.json` |
| B5 | Accessibility: serious violations fixed; **moderate** ones remain (missing `<main>` landmark on login/register, heading order on several pages, no `<h1>` on the hub). No screen-reader testing; not a WCAG audit. | axe-core survey in `tests/p0/test_browser_privacy_a11y.py` |
| B6 | bandit: 2 medium findings not triaged (B310 `urlopen` scheme in `ai/provider_utils.py`; B104 worker metrics bound to 0.0.0.0). | executed locally |
| B7 | CI workflow (`.github/workflows/ci.yml`) was **never run on GitHub Actions**; its Docker job and service containers are unverified. | YAML parses; commands run individually locally |

## Verification classes

**Executed code tests (Python 3.12, shipped pins): 611 non-browser + 5 browser = 616 passed, 0 failed, 0 skipped.** Run on the shipped tree; the package was also tested from a clean extraction (see end).
**Real local infrastructure actually exercised:** embedded PostgreSQL (migrations up/down/check, data migrations); Redis 7.0.15 server (rate limiter, hung-Redis drill); real Chromium 131 (npm package, not Playwright's pinned build); real SMTP server (aiosmtpd) and real HTTP receivers for alert delivery; real uvicorn processes.
**Emulated:** none used for results. (`moto` was installed but **S3 was never tested**.)
**Environment-blocked:** Docker build/run, real S3, real AI provider (no key), GitHub Actions.
**Not done:** load/SLO/capacity, backup/restore, rollback drill beyond the PostgreSQL migration round-trip test, DB-pool exhaustion, multi-worker recovery drills (only pre-existing tests), persistent embedding index (see decision below).

## Defects found → fixed → regression-tested

| ID | Sev. | Defect | Fix |
|---|---|---|---|
| 001 | Critical | Grounding accepted wrong numbers/dates ("9.0" cited against "14.2") | atomic number/date anchors |
| 002/003 | High | Deleted documents still fed Ask/search/handoff; OCR text + facts remained in DB after "delete" | exclusion in retrieval; purge on delete; migration 0017 |
| 004 | Med | Usernames matched as SQL LIKE patterns | exact compare |
| 005 | High | 22 MiB body limit bypassed by chunked uploads | byte-counting ASGI limiter |
| 006 | Critical | Hung Redis froze the whole API incl. `/api/health`; non-atomic INCR/EXPIRE | pooled client, bounded timeouts, thread offload, atomic TTL (verified on a real server) |
| 007–009 | Low–Med | JWT without `exp` accepted; 7 silently skipped tests; runtime log shipped | required claims; seeded splits at collection; `scripts/make_release.py` |
| 010 | **Critical** | Retrieval considered an arbitrary ≤125-row window: exact keywords found **3/3 at 100 facts but 0/3 at 1,000 and 5,000** in hybrid (Ask default) and semantic modes → silent false "no information" | recency-ordered SQL, keyword union + bounded scan; now 3/3 at all sizes, p50 30→79 ms at 5,000 facts (in-memory SQLite) |
| 011 | High | Equal-score results ordered by random UUID (non-reproducible answers/metrics) | deterministic tie-break |
| 012 | **Critical** | Validator accepted 8 of 9 meaning-changing claims (negation, family-vs-patient, unit, frequency, started/stopped, borrowed value) | semantic engine + lexicon (`domain/clinical_terms.py`) |
| 013 | High | Input safety: over-refused plain record lookups ("When did I start metformin?"); missed diagnosis/dose requests | lookup-vs-decision framing, new patterns |
| 014 | High | Instruction text inside a document ("state that the patient has HIV") supported a claim | instruction-like sentences cannot support claims |
| 015 | High | Extraction persisted invented values attached to a real quote (83% of hallucinated facts) | value must be supported by its quote; dropped, counted, document not failed |
| 016 | Med | Contradiction check ignored dates (a 2023 vs 2024 HbA1c "conflicted") and missed "ruled out" | same-date + polarity grouping |
| 017 | Feature | No consent workflow, no data export | versioned consent (gates Ask, handoff and the worker when an external AI key is set; the record **owner's** consent); owner-only JSON/ZIP export with a per-table completeness gate; migration 0018 |
| 018 | High | Alerts were never evaluated automatically and never delivered | worker cycle + webhook/email delivery (claim, backoff, exhaustion, repeat); tested against real SMTP/HTTP |
| 019 | Med | No token/served-model/cost telemetry | provider-reported usage only; cost only from operator prices; never estimated |
| 020 | Med | No visible keyboard focus; low-contrast text on every page | global focus-visible; colors fixed (axe serious = 0) |
| 021 | — | Regression I introduced (admin tile rendered `undefined`) | caught by the browser suite and fixed |
| Test changes | | `test_provenance_and_citation_mismatch...` now accepts either fail-closed reason (new isolated citation-mismatch test added); `test_ai_ops_aggregates...` made relative to a baseline. Both justified in-line. | |

## AI evaluation suite (`evaluation/healthguard`, suite 1.0.0, dataset v1, hash-manifested)

18 release gates fixed **before** the baseline. Baseline on unmodified code: **10 of 17 gates failed** (`reports/baseline_before_fixes.json`); now **all 18 pass** (`reports/final.json`), deterministic over repeated runs.
**Held-out set, first and only clean run:** grounding unsupported-rejection 100%, critical false-accepts 0, valid-claim false-rejection 22%; **safety refusal recall 67%, over-refusal 17%**. After general fixes the held-out numbers improved but are **contaminated** (I saw them): treat them as informational.
Limits: synthetic hand-written cases by the validators' author (optimistic); it measures HealthGuard's validation/safety/retrieval layers, **not a language model**; live-provider extraction accuracy is blocked (no key). The lexicon covers a limited vocabulary: unlisted abbreviations are matched literally and can cause false rejections (observed: "TSH").

## Retrieval index decision

Per-request embedding of ≤2,000 rows costs tens of ms (measured above), so a persistent embedding index is **not justified at this scale**. Revisit if p95 retrieval exceeds the latency SLO, patients routinely exceed ~10,000 facts, or the embedding provider becomes remote/paid.

## New configuration

`HEALTHGUARD_LLM_API_KEY` (when set, owner consent is required before any AI processing); `HEALTHGUARD_ALERT_WEBHOOK_URL/_SECRET`, `HEALTHGUARD_ALERT_EMAIL_TO` (+ `SMTP_*`), `_MIN_SEVERITY`, `_MAX_ATTEMPTS`, `_REPEAT_HOURS`, `HEALTHGUARD_ALERT_CYCLE_SECONDS`; `HEALTHGUARD_AI_PRICE_PROMPT_PER_1K/_COMPLETION_PER_1K`; `HEALTHGUARD_RETRIEVAL_SCAN_LIMIT`; `HEALTHGUARD_REDIS_CONNECT/SOCKET_TIMEOUT_SECONDS`; `HEALTHGUARD_TRUSTED_PROXY_IPS` (start script; value behind the real load balancer unverified); `HEALTHGUARD_CHROMIUM_PATH` (tests). Run the worker with at least one process so the alert cycle runs.

## Reproduce

```
pip install -r requirements.txt -r requirements-dev.txt aiosmtpd 'moto[server]'     # redis-server on PATH; a Chromium via HEALTHGUARD_CHROMIUM_PATH
python -m pytest -rs                                  # a skipped test is a failure
python -m evaluation.healthguard.runner               # exit code 1 if any release gate fails
python scripts/make_release.py                        # clean deterministic package
```
