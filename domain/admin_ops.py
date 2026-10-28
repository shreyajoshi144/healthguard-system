"""Operational (non-PHI) building blocks for the admin console.

Hard rules enforced here:
* Admin queries touch only operational tables (processing_jobs, ops_alerts,
  security_events, audit_logs *metadata*, users *account metadata*, admin_grants).
  They never import or query patient health tables.
* Output is built field-by-field into explicit DTO dicts. No ORM object is ever serialized.
* Secrets are never read into responses; configuration is reported as booleans.
"""
from __future__ import annotations

import os
import base64, hashlib, hmac, os, platform, re, struct, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from sqlalchemy import func, select, text, delete
from sqlalchemy.orm import Session
from domain.mailer import smtp_configured
from db.models import AdminGrant, AuditLog, DocumentStatus, OpsAlert, ProcessingJob, ReminderDelivery, SecurityEvent, User

MAX_ATTEMPTS = int(os.getenv("HEALTHGUARD_JOB_MAX_ATTEMPTS", "3"))
QUEUE_DEPTH_WARN = int(os.getenv("HEALTHGUARD_ALERT_QUEUE_DEPTH", "100"))
AUTH_FAIL_SPIKE = int(os.getenv("HEALTHGUARD_ALERT_AUTH_FAILURES_15M", "20"))
TOTP_STEP = 30

def utcnow() -> datetime:
    return datetime.now(timezone.utc)

def _aware(d: datetime | None) -> datetime | None:
    return d if d is None or d.tzinfo else d.replace(tzinfo=timezone.utc)

# ------------------------------------------------------------------ TOTP (RFC 6238, HMAC-SHA1, 6 digits)
def totp_key() -> str | None:
    key = os.getenv("HEALTHGUARD_ADMIN_TOTP_KEY", "")
    return key if len(key) >= 32 else None

def derive_secret(salt: str) -> bytes:
    key = totp_key()
    if not key:
        raise RuntimeError("HEALTHGUARD_ADMIN_TOTP_KEY (>=32 chars) is required for the admin console.")
    return hmac.new(key.encode(), salt.encode(), hashlib.sha256).digest()[:20]

def _hotp(secret: bytes, counter: int) -> str:
    digest = hmac.new(secret, struct.pack(">Q", counter), hashlib.sha1).digest()
    off = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[off:off + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{code:06d}"

def totp_now(salt: str, at: float | None = None) -> str:
    return _hotp(derive_secret(salt), int((at or time.time()) // TOTP_STEP))

def verify_totp(salt: str, code: str, last_step: int | None, at: float | None = None) -> int | None:
    """Return the matched time-step (for replay protection) or None."""
    if not re.fullmatch(r"\d{6}", code or ""):
        return None
    secret = derive_secret(salt)
    now_step = int((at or time.time()) // TOTP_STEP)
    for step in (now_step, now_step - 1, now_step + 1):
        if hmac.compare_digest(_hotp(secret, step), code) and (last_step is None or step > last_step):
            return step
    return None

def provisioning_uri(username: str, salt: str) -> str:
    secret = base64.b32encode(derive_secret(salt)).decode().rstrip("=")
    return f"otpauth://totp/HealthGuard%20Admin:{username}?secret={secret}&issuer=HealthGuard%20Admin"

# ------------------------------------------------------------------ dependency health (operational only)
def check_database(db: Session) -> dict:
    t0 = time.monotonic()
    try:
        db.execute(text("SELECT 1"))
        return {"status": "OK", "latency_ms": round((time.monotonic() - t0) * 1000, 2)}
    except Exception as exc:
        return {"status": "DOWN", "error_type": type(exc).__name__}

def _redis():
    from jobs.queue import _client
    return _client()

def check_redis_and_queue() -> tuple[dict, dict, dict]:
    if not os.getenv("REDIS_URL"):
        nc = {"status": "NOT_CONFIGURED"}
        return nc, {"status": "NOT_CONFIGURED"}, {"status": "NOT_CONFIGURED", "count": 0, "workers": []}
    from jobs.queue import QUEUE_KEY, PROCESSING_KEY, QUARANTINE_KEY, _close
    from jobs.worker import WORKER_HEARTBEAT_KEY_PREFIX
    client = None
    try:
        client = _redis()
        t0 = time.monotonic(); client.ping(); lat = round((time.monotonic() - t0) * 1000, 2)
        queue = {"status": "OK", "waiting": int(client.llen(QUEUE_KEY)), "in_flight": int(client.llen(PROCESSING_KEY)), "quarantined_messages": int(client.llen(QUARANTINE_KEY))}
        workers = []
        now = time.time()
        for key in client.scan_iter(match=f"{WORKER_HEARTBEAT_KEY_PREFIX}*", count=100):
            raw = client.get(key)
            try: age = max(0, int(now - int(raw)))
            except (TypeError, ValueError): continue
            workers.append({"worker": hashlib.sha256(str(key).encode()).hexdigest()[:8], "heartbeat_age_seconds": age})
        workers.sort(key=lambda w: w["worker"])
        return {"status": "OK", "latency_ms": lat}, queue, {"status": "OK" if workers else "NO_HEARTBEAT", "count": len(workers), "workers": workers[:50]}
    except Exception as exc:
        return {"status": "DOWN", "error_type": type(exc).__name__}, {"status": "UNKNOWN"}, {"status": "UNKNOWN", "count": 0, "workers": []}
    finally:
        if client is not None:
            from jobs.queue import _close
            _close(client)

def check_storage() -> dict:
    try:
        from storage.service import storage
        return {"status": "OK" if storage.health_check() else "DOWN", "backend": getattr(storage, "backend", "unknown")}
    except Exception as exc:
        return {"status": "DOWN", "error_type": type(exc).__name__}

def ai_config() -> dict:
    configured = all(os.getenv(k, "").strip() for k in ("HEALTHGUARD_LLM_BASE_URL", "HEALTHGUARD_LLM_API_KEY", "HEALTHGUARD_LLM_MODEL"))
    return {"status": "CONFIGURED" if configured else "NOT_CONFIGURED", "provider": os.getenv("HEALTHGUARD_LLM_PROVIDER", "openai_compatible"),
            "model": os.getenv("HEALTHGUARD_LLM_MODEL", "") or None, "live_probe": "NOT_PERFORMED",
            "note": "Availability is derived from call outcomes; no synthetic request is sent to the provider."}

def migration_status(db: Session) -> dict:
    head = None
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory
        root = Path(__file__).resolve().parents[1]
        head = ScriptDirectory.from_config(Config(str(root / "alembic.ini"))).get_current_head()
    except Exception:
        pass
    try:
        current = db.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    except Exception:
        return {"status": "UNKNOWN", "current": None, "head": head, "detail": "alembic_version table not present"}
    return {"status": "CURRENT" if head and current == head else "BEHIND_OR_DIVERGED", "current": current, "head": head}

def job_counts(db: Session) -> dict:
    now = utcnow()
    by = {s.value: 0 for s in DocumentStatus if s.value in {"QUEUED", "PROCESSING", "COMPLETED", "FAILED"}}
    for status, n in db.execute(select(ProcessingJob.status, func.count()).group_by(ProcessingJob.status)).all():
        by[status.value] = n
    stale = db.scalar(select(func.count()).select_from(ProcessingJob).where(ProcessingJob.status == DocumentStatus.PROCESSING, ProcessingJob.lease_expires_at.is_not(None), ProcessingJob.lease_expires_at < now)) or 0
    retrying = db.scalar(select(func.count()).select_from(ProcessingJob).where(ProcessingJob.status == DocumentStatus.QUEUED, ProcessingJob.attempts > 0)) or 0
    poisoned = db.scalar(select(func.count()).select_from(ProcessingJob).where(ProcessingJob.status == DocumentStatus.FAILED, ProcessingJob.attempts >= MAX_ATTEMPTS, ProcessingJob.quarantined_at.is_(None))) or 0
    quarantined = db.scalar(select(func.count()).select_from(ProcessingJob).where(ProcessingJob.quarantined_at.is_not(None))) or 0
    return {"by_status": by, "stale": stale, "retrying": retrying, "poisoned": poisoned, "quarantined": quarantined}

def reminder_counts(db: Session) -> dict:
    """Counts only: no recipients, patients or follow-up content."""
    now = utcnow(); day = now - timedelta(hours=24)
    by = {st: n for st, n in db.execute(select(ReminderDelivery.status, func.count()).group_by(ReminderDelivery.status)).all()}
    overdue = db.scalar(select(func.count()).select_from(ReminderDelivery).where(ReminderDelivery.status == "PENDING", ReminderDelivery.scheduled_for < now - timedelta(hours=1))) or 0
    failed_24h = db.scalar(select(func.count()).select_from(ReminderDelivery).where(ReminderDelivery.status == "FAILED", ReminderDelivery.updated_at >= day)) or 0
    return {"by_status": by, "overdue_pending": overdue, "failed_24h": failed_24h, "email_delivery_configured": smtp_configured()}

def config_checks() -> dict:
    env = os.getenv("APP_ENV", "development").lower()
    jwt = os.getenv("HEALTHGUARD_JWT_SECRET", "")
    return {"environment": env, "jwt_secret_configured": len(jwt) >= 32, "admin_totp_key_configured": totp_key() is not None,
            "secure_cookies": env == "production", "redis_configured": bool(os.getenv("REDIS_URL")), "metrics_token_configured": bool(os.getenv("HEALTHGUARD_METRICS_TOKEN")),
            "storage_backend": os.getenv("STORAGE_BACKEND", "local"), "smtp_configured": smtp_configured(), "database_dialect": os.getenv("DATABASE_URL", "sqlite").split(":", 1)[0].split("+")[0],
            "debug_enabled": os.getenv("DEBUG", "").lower() in {"1", "true", "yes"}}

def version_info() -> dict:
    return {"version": os.getenv("APP_VERSION", "unversioned"), "git_sha": (os.getenv("GIT_SHA") or "")[:12] or None, "environment": os.getenv("APP_ENV", "development").lower(),
            "python": platform.python_version(), "started_hint": None}

# ------------------------------------------------------------------ AI metrics (process-local counters; see "scope")
_LINE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+([-+0-9.eE]+|NaN|\+Inf)$')

def _parse_prometheus(text_: str) -> list[tuple[str, dict, float]]:
    rows = []
    for line in text_.splitlines():
        if line.startswith("#"): continue
        m = _LINE.match(line.strip())
        if not m: continue
        labels = dict(re.findall(r'(\w+)="((?:\\.|[^"\\])*)"', m.group(2) or ""))
        try: rows.append((m.group(1), labels, float(m.group(3))))
        except ValueError: pass
    return rows

def _tokens_and_cost(rows) -> dict:
    tok = {"prompt": 0.0, "completion": 0.0}; by_model: dict[str, dict] = {}; cost = 0.0; missing = 0.0
    for n, l, v in rows:
        if n == "healthguard_ai_tokens_total" and l.get("kind") in tok:
            tok[l["kind"]] += v; by_model.setdefault(l.get("model", "?"), {"prompt": 0, "completion": 0})[l["kind"]] += v
        elif n == "healthguard_ai_cost_micro_total": cost += v
        elif n == "healthguard_ai_usage_missing_total": missing += v
    priced = bool(os.getenv("HEALTHGUARD_AI_PRICE_PROMPT_PER_1K") and os.getenv("HEALTHGUARD_AI_PRICE_COMPLETION_PER_1K"))
    return {"available": bool(tok["prompt"] or tok["completion"]), "basis": "token counts reported by the provider in each response; calls without usage are counted separately and never estimated",
            "tokens": {"prompt": int(tok["prompt"]), "completion": int(tok["completion"])}, "by_model": by_model, "calls_without_usage": int(missing),
            "cost": ({"available": True, "micro_units": int(cost), "basis": "operator-configured per-1K-token prices x provider-reported tokens (currency is whatever the operator priced in)"} if priced else
                     {"available": False, "reason": "No per-token prices are configured (HEALTHGUARD_AI_PRICE_PROMPT_PER_1K / HEALTHGUARD_AI_PRICE_COMPLETION_PER_1K); no cost is reported rather than estimating one."})}


def _served_models(rows) -> dict:
    out: dict[str, list[str]] = {}
    for n, l, v in rows:
        if n == "healthguard_ai_served_model_total" and v:
            out.setdefault(l.get("requested", "?"), [])
            if l.get("served") not in out[l.get("requested", "?")]: out[l.get("requested", "?")].append(l.get("served", "?"))
    return out


def ai_metrics() -> dict:
    from domain.observability import prometheus
    rows = _parse_prometheus(prometheus())
    def total(name, **flt):
        return sum(v for n, l, v in rows if n == name and all(l.get(k) == x for k, x in flt.items()))
    calls_ok = total("healthguard_ai_provider_calls_total", outcome="success"); calls_fail = total("healthguard_ai_provider_calls_total", outcome="failure")
    lat_sum = total("healthguard_ai_provider_latency_sum"); lat_cnt = total("healthguard_ai_provider_latency_count")
    attempts = {}
    for n, l, v in rows:
        if n == "healthguard_ai_provider_attempts_total":
            key = l.get("error_type") or (f"http_{l.get('status')}" if l.get("status") else l.get("outcome", "unknown"))
            attempts[key] = attempts.get(key, 0) + v
    grounding = {}
    for n, l, v in rows:
        if n == "healthguard_ai_grounding_rejections_total":
            k = f"{l.get('surface','?')}:{l.get('reason','?')}"; grounding[k] = grounding.get(k, 0) + v
    surfaces = lambda name: {l.get("surface", "?"): v for n, l, v in rows if n == name}
    done = calls_ok + calls_fail
    return {"scope": "this API process since start (counters reset on restart; per-process unless PROMETHEUS_MULTIPROC_DIR is set)",
            "provider": ai_config(), "requests": {"succeeded": calls_ok, "failed": calls_fail, "total": done},
            "availability_ratio": round(calls_ok / done, 4) if done else None,
            "latency_ms": {"count": lat_cnt, "average": round(lat_sum / lat_cnt, 1) if lat_cnt else None},
            "failed_attempts_by_type": attempts, "timeouts_or_retries": sum(v for k, v in attempts.items() if "Timeout" in k or k in {"http_408", "http_429", "http_500", "http_502", "http_503", "http_504"}),
            "safety_rejections_by_surface": surfaces("healthguard_ai_safety_rejections_total"), "grounding_rejections": grounding,
            "insufficient_evidence_by_surface": surfaces("healthguard_ai_insufficient_evidence_total"),
            "tokens_and_cost": _tokens_and_cost(rows),
            "served_models": _served_models(rows)}

# ------------------------------------------------------------------ alerts
def _upsert_alert(db: Session, fingerprint: str, source: str, severity: str, title: str, detail: str | None) -> None:
    now = utcnow()
    row = db.scalar(select(OpsAlert).where(OpsAlert.fingerprint == fingerprint, OpsAlert.status.in_(["OPEN", "ACKNOWLEDGED"])))
    if row:
        row.last_seen_at = now; row.occurrences += 1; row.severity = severity; row.detail = detail
    else:
        db.add(OpsAlert(fingerprint=fingerprint, source=source, severity=severity, title=title[:160], detail=(detail or None) and detail[:300], status="OPEN", first_seen_at=now, last_seen_at=now))

def evaluate_alerts(db: Session) -> dict:
    """Derive alerts from real, current conditions. Delivery is never simulated: delivery_state stays NOT_CONFIGURED until a channel is configured
    (see domain.alert_delivery); the worker runs this evaluation periodically so alerts do not depend on an admin clicking."""
    jobs = job_counts(db)
    redis_s, queue, workers = check_redis_and_queue()
    storage_s = check_storage(); mig = migration_status(db); cfg = config_checks()
    firing: dict[str, tuple[str, str, str, str | None]] = {}
    failed = jobs["by_status"].get("FAILED", 0)
    if failed: firing["jobs.failed"] = ("jobs", "CRITICAL" if failed >= 5 else "WARNING", f"{failed} document-processing job(s) failed", None)
    if jobs["stale"]: firing["jobs.stale"] = ("jobs", "WARNING", f"{jobs['stale']} job(s) hold an expired lease", "Worker may have died mid-job; recovery or manual retry required.")
    if jobs["poisoned"]: firing["jobs.poisoned"] = ("jobs", "CRITICAL", f"{jobs['poisoned']} job(s) exhausted all attempts", None)
    if jobs["quarantined"]: firing["jobs.quarantined"] = ("jobs", "INFO", f"{jobs['quarantined']} job(s) are quarantined", None)
    if redis_s["status"] == "DOWN": firing["redis.down"] = ("redis", "CRITICAL", "Redis is unreachable", redis_s.get("error_type"))
    if queue.get("status") == "OK" and queue["waiting"] > QUEUE_DEPTH_WARN: firing["queue.depth"] = ("queue", "WARNING", f"Queue depth {queue['waiting']} exceeds {QUEUE_DEPTH_WARN}", None)
    if queue.get("status") == "OK" and queue["waiting"] + queue["in_flight"] > 0 and workers["count"] == 0: firing["workers.none"] = ("workers", "CRITICAL", "Jobs are waiting but no worker heartbeat is present", None)
    if storage_s["status"] != "OK": firing["storage.down"] = ("storage", "CRITICAL", "Object storage health check failed", storage_s.get("error_type"))
    if mig["status"] == "BEHIND_OR_DIVERGED": firing["db.migration"] = ("database", "CRITICAL", "Database schema is not at the migration head", f"current={mig['current']} head={mig['head']}")
    if cfg["environment"] == "production" and not ai_config()["status"] == "CONFIGURED": firing["ai.not_configured"] = ("ai", "WARNING", "AI provider is not configured in production", None)
    rem = reminder_counts(db)
    if rem["failed_24h"]: firing["reminders.failed"] = ("reminders", "WARNING", f"{rem['failed_24h']} reminder email(s) failed in the last 24 hours", None)
    if rem["overdue_pending"]: firing["reminders.overdue"] = ("reminders", "WARNING", f"{rem['overdue_pending']} reminder(s) are more than 1 hour overdue", "The worker may not be dispatching reminders.")
    since = utcnow() - timedelta(minutes=15)
    auth_fail = db.scalar(select(func.count()).select_from(SecurityEvent).where(SecurityEvent.event_type == "AUTH_FAILURE", SecurityEvent.occurred_at >= since)) or 0
    if auth_fail >= AUTH_FAIL_SPIKE: firing["security.auth_spike"] = ("security", "WARNING", f"{auth_fail} authentication failures in 15 minutes", None)
    for fp, (src, sev, title, detail) in firing.items():
        _upsert_alert(db, fp, src, sev, title, detail)
    resolved = 0
    for row in db.scalars(select(OpsAlert).where(OpsAlert.status.in_(["OPEN", "ACKNOWLEDGED"]))).all():
        if row.fingerprint not in firing:
            row.status = "RESOLVED"; row.resolved_at = utcnow(); resolved += 1
    from domain.security_events import prune
    prune(db)
    db.commit()
    return {"firing": len(firing), "auto_resolved": resolved}

# ------------------------------------------------------------------ DTOs
def mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    return (local[:1] + "***@" + domain) if domain else "***"

def short_ref(value: Any) -> str | None:
    return hashlib.sha256(str(value).encode()).hexdigest()[:10] if value else None

def job_dto(job: ProcessingJob) -> dict:
    now = utcnow()
    lease = _aware(job.lease_expires_at)
    stale = job.status == DocumentStatus.PROCESSING and lease is not None and lease < now
    poisoned = job.status == DocumentStatus.FAILED and job.attempts >= MAX_ATTEMPTS and job.quarantined_at is None
    state = "QUARANTINED" if job.quarantined_at else "STALE" if stale else "POISONED" if poisoned else "RETRYING" if (job.status == DocumentStatus.QUEUED and job.attempts > 0) else job.status.value
    created, updated = _aware(job.created_at), _aware(job.updated_at)
    return {"id": str(job.id), "state": state, "status": job.status.value, "attempts": job.attempts, "max_attempts": MAX_ATTEMPTS, "last_error_code": job.last_error,
            "created_at": created, "updated_at": updated, "lease_expires_at": lease, "next_attempt_at": _aware(job.next_attempt_at),
            "age_seconds": int((now - created).total_seconds()) if created else None,
            "processing_seconds": int((updated - created).total_seconds()) if (job.status == DocumentStatus.COMPLETED and created and updated) else None,
            "heartbeat_age_seconds": int((now - updated).total_seconds()) if (job.status == DocumentStatus.PROCESSING and updated) else None,
            "quarantined_at": _aware(job.quarantined_at), "quarantine_reason": job.quarantine_reason, "document_ref": short_ref(job.document_id)}

def alert_dto(a: OpsAlert) -> dict:
    return {"id": str(a.id), "source": a.source, "severity": a.severity, "title": a.title, "detail": a.detail, "status": a.status, "occurrences": a.occurrences,
            "first_seen_at": _aware(a.first_seen_at), "last_seen_at": _aware(a.last_seen_at), "acknowledged_at": _aware(a.acknowledged_at), "resolved_at": _aware(a.resolved_at),
            "delivery": {"state": a.delivery_state, "attempts": a.delivery_attempts, "last_attempt_at": _aware(a.last_delivery_at)}}

def user_dto(u: User, patient_count: int, admin: bool) -> dict:
    return {"id": str(u.id), "username": u.username, "email_masked": mask_email(u.email), "status": u.status.value, "email_verified": u.email_verified, "deletion_pending": u.deletion_pending,
            "session_version": u.session_version, "created_at": _aware(u.created_at), "updated_at": _aware(u.updated_at), "patient_profile_count": patient_count, "is_admin": admin}

def security_event_dto(e: SecurityEvent) -> dict:
    return {"id": str(e.id), "occurred_at": _aware(e.occurred_at), "event_type": e.event_type, "route": e.route, "method": e.method, "status_code": e.status_code,
            "actor_ref": short_ref(e.actor_user_id), "client_ref": e.client_hash}

def audit_dto(r: AuditLog) -> dict:       # metadata only: context is intentionally never returned
    return {"id": str(r.id), "occurred_at": _aware(r.occurred_at), "action": r.action, "resource_type": r.resource_type, "actor_ref": short_ref(r.actor_user_id)}
