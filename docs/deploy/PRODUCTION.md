# HealthGuard Production Deployment

> The checklist below is deliberately terse. [`docs/DEPLOYMENT.md`](../docs/DEPLOYMENT.md) explains the same system with diagrams, and [`docs/OPERATIONS.md`](../docs/OPERATIONS.md) covers running it.

## Topology

- **API:** FastAPI/Uvicorn, serves the HealthGuard frontend and `/api/*` endpoints.
- **Worker:** separate `python -m jobs.worker` process for document processing.
- **PostgreSQL:** authoritative application database and `ProcessingJob` source of truth.
- **Redis:** queue/delivery, rate limiting, worker heartbeat, and recoverable transient state; never the durable job authority.
- **Private object storage:** S3-compatible bucket; documents are never served from the frontend/static tree.
- **AI provider:** server-side OpenAI-compatible endpoint; credentials never reach the browser.
- **SMTP:** one relay (`SMTP_*`) used by account verification, follow-up reminders and operational alert email — all three go through `domain/mailer.py`. The API sends verification mail; the **worker** sends reminders and alert mail, so both services need identical `SMTP_*` and `HEALTHGUARD_PUBLIC_BASE_URL`.
- **OCR:** Tesseract (`tesseract-ocr`, English data) runs in the worker. It is installed by the `Dockerfile`; Render's native Python runtime cannot install it, which is why both Render services use `runtime: docker`.
- **Prometheus:** scrape the protected API metrics endpoint and the protected worker `/metrics` endpoint as separate targets. Render uses separate services/processes, so it does **not** use `PROMETHEUS_MULTIPROC_DIR` across API and worker. Docker Compose keeps the existing shared-volume multiprocess setup because both services share one host volume.

## Required production configuration

Required secrets/infrastructure values include:

`APP_ENV=production`, `DATABASE_URL` (PostgreSQL), `REDIS_URL`, `STORAGE_BACKEND=s3`, HTTPS `STORAGE_ENDPOINT`, `STORAGE_BUCKET`, storage credentials, `HEALTHGUARD_JWT_SECRET`, `HEALTHGUARD_PUBLIC_BASE_URL` (HTTPS), `CORS_ALLOWED_ORIGINS` (explicit HTTPS origins), `HEALTHGUARD_LLM_PROVIDER=openai_compatible`, HTTPS LLM base URL/model/key, explicit production embedding provider/base URL/model/key, SMTP host/from and TLS, `HEALTHGUARD_METRICS_TOKEN` (>=32 chars). Placeholder values from `.env.example` (anything containing `replace-with`) are rejected at startup, as are a development SMTP host (`localhost`, `mailpit`), `SMTP_PORT=465`, `SMTP_STARTTLS=false`, a username without a password (or vice versa), and an `HEALTHGUARD_ADMIN_TOTP_KEY` shorter than 32 characters. The worker runs the same checks (and refuses to start without a working Tesseract) before it opens its health port. `PROMETHEUS_MULTIPROC_DIR` is only required for the Docker Compose shared-volume deployment; do not configure it as a shared directory between Render services.

Do not commit real values. Inject secrets through the deployment platform/secret manager.

## Release procedure

1. Provision PostgreSQL, Redis, private S3-compatible storage, AI provider, and SMTP.
2. Create the production environment with the required variables above.
3. Run the release migration step exactly once: `scripts/migrate.sh` (which executes `alembic upgrade head`) (Render uses the API service `preDeployCommand`; Compose uses the `migrate` service).
4. Verify the database is at Alembic head with `scripts/check-schema.py`. API and worker startup only verify schema state; they do not run migrations.
5. Start the API with `scripts/production-start.sh`.
6. Start the worker with `scripts/production-worker.sh`.
7. Verify `GET /api/health` returns liveness `200`.
8. Verify `GET /api/ready` returns `200` only when required dependencies are usable.
9. Scrape `/api/ops/metrics` using the configured metrics token from a protected monitoring path.
10. Scrape the Render worker `/metrics` endpoint separately with the same protected metrics token; use `/health` only for worker liveness.
11. Verify worker heartbeat and queue/job metrics.
12. Run the deployment smoke checks before routing production traffic.

The API also refuses production startup when the database is not at the current Alembic head.

## Local development (Docker Compose)

```bash
cp .env.example .env        # development values only; nothing here is a production secret
docker compose up --build   # postgres, redis, minio, mailpit, migrate, api, worker
```

* App: http://127.0.0.1:8000 · **Dev mailbox (Mailpit): http://127.0.0.1:8025** — register, then open the verification email there. SMTP is `mailpit:1025` (no TLS/auth). No paid domain or account is needed and nothing is delivered outside your machine. Reminders and alert email land in the same mailbox.
* `DATABASE_URL`'s password must equal `POSTGRES_PASSWORD`, and `STORAGE_*` credentials must equal `MINIO_ROOT_*` (the shipped `.env.example` does). `HEALTHGUARD_METRICS_TOKEN` must be non-empty: the start scripts refuse an empty value.
* To reset the database after changing `POSTGRES_PASSWORD`: `docker compose down -v`.

## Render (Docker runtime)

`render.yaml` defines two Docker web services built from the repository `Dockerfile`: `healthguard-api` (`dockerCommand ./scripts/production-start.sh`, `preDeployCommand ./scripts/migrate.sh`, health check `/api/health`, `PORT=8000`) and `healthguard-worker` (`dockerCommand ./scripts/production-worker.sh`, health check `/health` on Render's injected `PORT`; it is a web service only so Render can health-check it — `/metrics` needs `X-Metrics-Token`).

1. Create the PostgreSQL and Redis instances and a private S3-compatible bucket; create the Blueprint from `render.yaml`.
2. Fill every `sync: false` variable on **both** services where the worker lists it (`DATABASE_URL`, `REDIS_URL`, `STORAGE_*`, `HEALTHGUARD_JWT_SECRET`, `HEALTHGUARD_METRICS_TOKEN`, LLM/embedding settings, and `SMTP_*` + `HEALTHGUARD_PUBLIC_BASE_URL`). Use identical values for shared variables. Do not set `PROMETHEUS_MULTIPROC_DIR` on Render.
3. Set `HEALTHGUARD_TRUSTED_PROXY_IPS` on the API to Render's proxy address(es) if you need per-client rate limiting (otherwise all anonymous clients share one bucket; the start script warns).
4. Deploy. The API's pre-deploy migration runs first; the worker refuses to start if migrations are not at head, if SMTP/public-URL/secret checks fail, or if Tesseract is not runnable.

### Email provider (production)

Any STARTTLS relay works. For Resend over SMTP: `SMTP_HOST=smtp.resend.com`, `SMTP_PORT=587`, `SMTP_USERNAME=resend`, `SMTP_PASSWORD=<Resend API key>`, `SMTP_STARTTLS=true`, and `SMTP_FROM` on a domain verified in Resend. Implicit-TLS port 465 is not supported. Delivery through a real provider has **not** been verified from this repository's test environment: after deploying, register a test account and confirm the verification email arrives, then confirm a reminder and an alert email (set `HEALTHGUARD_ALERT_EMAIL_TO`) from the admin console.

## Backup and recovery

### PostgreSQL

Use provider-native automated backups plus periodic logical backups (`pg_dump`). Test restores into an isolated PostgreSQL instance. Restore the database before starting workers so `ProcessingJob` remains authoritative.

### Object storage

Enable provider-side versioning/retention where appropriate and back up/replicate the private bucket according to the organization's retention policy. Do not make the bucket public.

### Redis

Redis is recoverable delivery state, not the authoritative job store. After Redis loss, restart Redis and workers; queued/retryable `ProcessingJob` rows are re-enqueued by the worker, and stale processing leases are recovered.

### Migration recovery

Take a PostgreSQL backup before schema migrations. Apply migrations before API traffic. If a migration fails, keep the API out of service, restore/reconcile the database according to the migration's documented rollback characteristics, then retry from a verified backup/state.

## Health and diagnostics

- `/api/health`: API process liveness only.
- `/api/ready`: bounded DB/Redis/storage readiness checks.
- API `/api/ops/metrics`: protected Prometheus exposition for the API process.
- Render worker `/health`: worker process liveness.
- Render worker `/metrics`: separately protected Prometheus exposition for worker-process metrics.
- Worker heartbeat: Redis `healthguard:worker:heartbeat:<worker-id>` with bounded TTL.
- Docker Compose: API and worker share the metrics volume and use the existing multiprocess collector.

Operational logs are structured and redacted. Do not put PHI, prompts, model responses, tokens, cookies, or credentials into log messages.

## Common recovery

- **Redis unavailable:** keep the worker running; restore Redis and allow automatic reconnect/re-enqueue/recovery.
- **Worker stopped:** restart the worker; expired DB leases and queued jobs are recoverable.
- **Storage unavailable:** readiness fails; document deletion/upload state remains governed by the existing durable recovery semantics.
- **AI provider unavailable:** AI calls fail safely; existing retry/grounding/safety behavior remains authoritative.
- **Database unavailable:** readiness fails and production startup refuses an unavailable database.

## Manual deployment requirements

Actual cloud credentials, DNS, TLS certificates, PostgreSQL/Redis/S3/SMTP/AI provider provisioning, secret-manager wiring, Prometheus scraping, backup schedules, restore drills, and load-balancer configuration must be completed by the deployment operator. This repository does not pretend those external resources are already configured.

### Browser E2E validation

Install the Playwright browser in the test environment before running the browser suite:

```bash
python -m playwright install chromium
```

Do not modify, skip, or weaken the browser test when Chromium is unavailable; report the environment limitation separately.

## Admin / operations console (P0)

* Console: `/admin` (UI) and `/api/admin/*`. Default deny: authenticated user -> active `admin_grants` row -> TOTP step-up session (30 min, bound to user + grant + session version) -> role (VIEWER read-only, OPERATOR for audited actions).
* There is no API to create an admin. Provision offline: `python scripts/admin.py grant <username> --role OPERATOR` (prints the TOTP provisioning URI once), `revoke`, `list`. Requires `DATABASE_URL` and `HEALTHGUARD_ADMIN_TOTP_KEY`.
* Without `HEALTHGUARD_ADMIN_TOTP_KEY` every `/api/admin/*` route returns 404.
* The console exposes operational data only (jobs, health, alerts, security events, masked account metadata). No patient-data route exists under `/api/admin`; admins have no extra access to patient resources.
* Alerts are derived from real conditions and evaluated automatically by the worker. They are delivered by signed webhook and/or email when `HEALTHGUARD_ALERT_WEBHOOK_URL` / `HEALTHGUARD_ALERT_EMAIL_TO` are set; with neither configured, `delivery_state` stays `NOT_CONFIGURED` and alerts are visible only in the admin console. See `docs/OPERATIONS.md`.
* Migrations `0012`–`0014` add measurements, admin/alert/security tables, job quarantine and follow-up confirmation. `alembic/env.py` widens `alembic_version.version_num` on PostgreSQL (revision ids exceed 32 chars).

## Email reminders (follow-ups)

* Email only (no SMS/push). Users opt in per patient profile (`PUT /api/patients/{id}/reminder-preferences`): lead times (on the day, 1/2/3 days, 1/2 weeks; max 4), time of day, time zone, and whether the follow-up title may appear in the email (default: no health details).
* Only EXACT-dated follow-ups that the user created or confirmed are reminded. Document-derived suggestions awaiting confirmation, approximate/year/unknown dates, completed and cancelled actions never produce email.
* Dispatch runs inside the existing worker loop (`jobs/worker.py`, `reminder_tick`), backed by the `reminder_deliveries` table: atomic lease claims (safe with multiple workers), exponential backoff (60s doubling, capped at 1h, 5 attempts), permanent-failure short-circuit, expiry of reminders more than 12h late, and lease recovery after a worker crash. Delivery is at-least-once: a crash between SMTP acceptance and the status write can re-send once; the deterministic `Message-ID` (`<reminder-{delivery id}@host>`) makes a duplicate detectable.
* Authorization is re-checked at send time (revoked/expired access, removed HEALTH_ACTIONS scope, unverified/deactivated account, patient deletion => cancelled, never sent).
* Run at least one worker (`python -m jobs.worker`) with SMTP configured. Admin console: Overview shows reminder counts; alerts fire on failed (24h) and overdue-pending reminders.
* Real mail-provider delivery has NOT been verified; tests use a protocol-level SMTP capture server. The worker needs the same `SMTP_*` and `HEALTHGUARD_PUBLIC_BASE_URL` as the API (see Render section) and validates them at startup.
