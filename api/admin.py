"""Admin / operations console API.

Access model (default deny): authenticated user -> active AdminGrant (created only by
`scripts/admin.py`, never via API) -> TOTP step-up session cookie (30 min, bound to the
user, grant and session version) -> role check (VIEWER read-only, OPERATOR for actions).
This router imports no patient-data model and exposes no patient-data endpoint.
"""
from __future__ import annotations
import os
from datetime import datetime, timedelta, timezone
from uuid import UUID
import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session
from backend.auth import get_current_user, SECRET_KEY, ALGORITHM
from db.models import AdminGrant, AdminRole, AuditLog, DocumentStatus, OpsAlert, PatientProfile, ProcessingJob, SecurityEvent, User, Document
from domain.audit import audit
from domain import admin_ops as ops
from domain import security_events
from domain.rate_limit import rate_limit
from utils.sql import escape_like
from db.session import get_db

router = APIRouter(prefix="/api/admin")
ADMIN_COOKIE = "hg_admin"
SESSION_MINUTES = 30
QUARANTINE_REASONS = {"POISON_SUSPECTED", "REPEATED_FAILURE", "MANUAL_REVIEW"}

class AdminCtx:
    def __init__(self, user: User, grant: AdminGrant):
        self.user, self.grant = user, grant
    @property
    def can_operate(self) -> bool:
        return self.grant.role == AdminRole.OPERATOR

def _deny(request: Request, user: User | None, status: int, detail: str):
    security_events.record(status=status, route=security_events.route_label(request), method=request.method, ip=request.client.host if request.client else None,
                           actor_user_id=user.id if user else None, request_id=getattr(request.state, "request_id", None), event_type="ADMIN_ACCESS_DENIED" if status == 403 else None)
    raise HTTPException(status, detail)

def _grant_for(db: Session, user: User) -> AdminGrant | None:
    return db.scalar(select(AdminGrant).where(AdminGrant.user_id == user.id, AdminGrant.revoked_at.is_(None)))

def _issue_cookie(response: Response, user: User, grant: AdminGrant) -> None:
    exp = datetime.now(timezone.utc) + timedelta(minutes=SESSION_MINUTES)
    token = jwt.encode({"sub": str(user.id), "typ": "admin", "gid": str(grant.id), "sv": int(user.session_version), "exp": exp}, SECRET_KEY, algorithm=ALGORITHM)
    response.set_cookie(ADMIN_COOKIE, token, max_age=SESSION_MINUTES * 60, httponly=True, secure=os.getenv("APP_ENV", "development").lower() == "production", samesite="strict", path="/api/admin")

def _guard(require_operator: bool):
    def dep(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> AdminCtx:
        if ops.totp_key() is None:
            raise HTTPException(404, "Not found.")                       # console is invisible until configured
        grant = _grant_for(db, user)
        if not grant:
            _deny(request, user, 403, "Admin access required.")
        token = request.cookies.get(ADMIN_COOKIE)
        try:
            claims = jwt.decode(token or "", SECRET_KEY, algorithms=[ALGORITHM])
            ok = claims.get("typ") == "admin" and claims.get("sub") == str(user.id) and claims.get("gid") == str(grant.id) and claims.get("sv") == int(user.session_version)
        except jwt.PyJWTError:
            ok = False
        if not ok:
            raise HTTPException(428, "ADMIN_STEP_UP_REQUIRED")
        if require_operator and grant.role != AdminRole.OPERATOR:
            _deny(request, user, 403, "Operator role required.")
        return AdminCtx(user, grant)
    return dep

viewer = _guard(False)
operator = _guard(True)

# ---------------------------------------------------------------- session
class SessionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")

@router.post("/session")
def start_session(payload: SessionIn, request: Request, response: Response, db: Session = Depends(get_db), user: User = Depends(get_current_user), _rl=Depends(rate_limit("admin_session", 5))):
    if ops.totp_key() is None:
        raise HTTPException(404, "Not found.")
    grant = _grant_for(db, user)
    if not grant:
        _deny(request, user, 403, "Admin access required.")
    step = ops.verify_totp(grant.totp_salt, payload.code, grant.last_totp_step)
    if step is None:
        audit(db, user, "ADMIN_SESSION_FAILED", "admin_session", grant.id); db.commit()
        security_events.record(status=401, route="/api/admin/session", method="POST", ip=request.client.host if request.client else None, actor_user_id=user.id, event_type="ADMIN_LOGIN_FAILED")
        raise HTTPException(403, "Invalid or already-used code.")
    # Single-use code: the step is recorded atomically so a replay (same or earlier step) fails.
    res = db.execute(update(AdminGrant).where(AdminGrant.id == grant.id, (AdminGrant.last_totp_step.is_(None)) | (AdminGrant.last_totp_step < step)).values(last_totp_step=step))
    if res.rowcount != 1:
        db.rollback(); raise HTTPException(403, "Invalid or already-used code.")
    audit(db, user, "ADMIN_SESSION_STARTED", "admin_session", grant.id, context={"role": grant.role.value}); db.commit()
    _issue_cookie(response, user, grant)
    return {"role": grant.role.value, "expires_in_seconds": SESSION_MINUTES * 60}

@router.delete("/session", status_code=204)
def end_session(response: Response, ctx: AdminCtx = Depends(viewer), db: Session = Depends(get_db)):
    audit(db, ctx.user, "ADMIN_SESSION_ENDED", "admin_session", ctx.grant.id); db.commit()
    response.delete_cookie(ADMIN_COOKIE, path="/api/admin")

@router.get("/me")
def me(ctx: AdminCtx = Depends(viewer)):
    return {"username": ctx.user.username, "role": ctx.grant.role.value, "can_operate": ctx.can_operate}

@router.get("/probe")
def probe(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Lets the console decide between 'not an admin' and 'needs TOTP' without leaking anything."""
    if ops.totp_key() is None or not _grant_for(db, user):
        raise HTTPException(404, "Not found.")
    try:
        claims = jwt.decode(request.cookies.get(ADMIN_COOKIE) or "", SECRET_KEY, algorithms=[ALGORITHM]); active = claims.get("sub") == str(user.id)
    except jwt.PyJWTError:
        active = False
    return {"step_up_required": not active}

# ---------------------------------------------------------------- overview / deployment
@router.get("/overview")
def overview(ctx: AdminCtx = Depends(viewer), db: Session = Depends(get_db)):
    redis_s, queue, workers = ops.check_redis_and_queue()
    jobs = ops.job_counts(db)
    open_alerts = db.execute(select(OpsAlert.severity, func.count()).where(OpsAlert.status.in_(["OPEN", "ACKNOWLEDGED"])).group_by(OpsAlert.severity)).all()
    components = {"api": {"status": "OK"}, "database": ops.check_database(db), "redis": redis_s, "storage": ops.check_storage(), "ai": ops.ai_config(), "workers": workers, "queue": queue}
    return {"generated_at": datetime.now(timezone.utc), "components": components, "jobs": jobs, "reminders": ops.reminder_counts(db), "version": ops.version_info(), "migration": ops.migration_status(db),
            "open_alerts": {sev: n for sev, n in open_alerts}, "unverified": ["real multi-worker Redis behaviour", "real S3", "real external AI provider", "real alert delivery", "200-user capacity", "PostgreSQL pool exhaustion"]}

@router.get("/deployment")
def deployment(ctx: AdminCtx = Depends(viewer), db: Session = Depends(get_db)):
    _, queue, workers = ops.check_redis_and_queue()
    cfg = ops.config_checks()
    problems = []
    if cfg["environment"] == "production":
        for key, label in [("jwt_secret_configured", "JWT secret"), ("redis_configured", "Redis"), ("metrics_token_configured", "Metrics token"), ("secure_cookies", "Secure cookies")]:
            if not cfg[key]: problems.append(f"{label} not configured")
        if cfg["debug_enabled"]: problems.append("DEBUG is enabled")
        if cfg["storage_backend"] != "s3": problems.append("Storage backend is not s3")
    return {"version": ops.version_info(), "migration": ops.migration_status(db), "workers": workers, "queue": queue, "configuration": cfg, "configuration_problems": problems,
            "healthy": not problems and ops.migration_status(db)["status"] in {"CURRENT", "UNKNOWN"}, "secrets": "never exposed; configuration is reported as booleans only"}

# ---------------------------------------------------------------- jobs
@router.get("/jobs")
def jobs(state: str | None = Query(None, pattern="^(QUEUED|PROCESSING|COMPLETED|FAILED|RETRYING|STALE|POISONED|QUARANTINED)$"), page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100), ctx: AdminCtx = Depends(viewer), db: Session = Depends(get_db)):
    now = datetime.now(timezone.utc)
    stmt = select(ProcessingJob)
    if state == "STALE": stmt = stmt.where(ProcessingJob.status == DocumentStatus.PROCESSING, ProcessingJob.lease_expires_at < now)
    elif state == "RETRYING": stmt = stmt.where(ProcessingJob.status == DocumentStatus.QUEUED, ProcessingJob.attempts > 0)
    elif state == "POISONED": stmt = stmt.where(ProcessingJob.status == DocumentStatus.FAILED, ProcessingJob.attempts >= ops.MAX_ATTEMPTS, ProcessingJob.quarantined_at.is_(None))
    elif state == "QUARANTINED": stmt = stmt.where(ProcessingJob.quarantined_at.is_not(None))
    elif state: stmt = stmt.where(ProcessingJob.status == DocumentStatus(state))
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.scalars(stmt.order_by(ProcessingJob.updated_at.desc(), ProcessingJob.id).offset((page - 1) * page_size).limit(page_size)).all()
    return {"total": total, "page": page, "page_size": page_size, "counts": ops.job_counts(db), "items": [ops.job_dto(j) for j in rows], "can_operate": ctx.can_operate}

def _job_or_404(db, job_id: UUID) -> ProcessingJob:
    job = db.get(ProcessingJob, job_id)
    if not job: raise HTTPException(404, "Job not found.")
    return job

@router.post("/jobs/{job_id}/retry")
def retry_job(job_id: UUID, ctx: AdminCtx = Depends(operator), db: Session = Depends(get_db), _rl=Depends(rate_limit("admin_job_action", 30))):
    job = _job_or_404(db, job_id)
    if job.quarantined_at: raise HTTPException(409, "Quarantined jobs must be released before retry.")
    before = ops.job_dto(job)["state"]
    if job.status == DocumentStatus.QUEUED:
        return {"id": str(job.id), "state": before, "already_queued": True}
    retryable = job.status == DocumentStatus.FAILED or before == "STALE"
    if not retryable: raise HTTPException(409, "Only failed or stale jobs can be retried.")
    doc = db.get(Document, job.document_id)
    if not doc or doc.status in (DocumentStatus.DELETED, DocumentStatus.DELETION_PENDING): raise HTTPException(409, "The source document is no longer available.")
    allowed_status = job.status
    res = db.execute(update(ProcessingJob).where(ProcessingJob.id == job.id, ProcessingJob.status == allowed_status, ProcessingJob.quarantined_at.is_(None))
                     .values(status=DocumentStatus.QUEUED, attempts=0 if before == "POISONED" else ProcessingJob.attempts, next_attempt_at=None, lease_expires_at=None, last_error=None))
    if res.rowcount != 1:
        db.rollback(); raise HTTPException(409, "The job changed state; refresh and retry.")
    doc.status = DocumentStatus.QUEUED
    audit(db, ctx.user, "ADMIN_JOB_RETRIED", "processing_job", job.id, context={"previous_state": before, "attempts_reset": before == "POISONED"}); db.commit()
    try:
        from jobs.queue import enqueue_document, QueueUnavailable
        enqueue_document(str(job.id))
    except Exception as exc:
        raise HTTPException(503, "Job was re-queued durably but the queue is unavailable; it will be picked up when the queue recovers.") from exc
    return {"id": str(job.id), "state": "QUEUED", "previous_state": before}

class QuarantineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(pattern="^(POISON_SUSPECTED|REPEATED_FAILURE|MANUAL_REVIEW)$")

@router.post("/jobs/{job_id}/quarantine")
def quarantine_job(job_id: UUID, payload: QuarantineIn, ctx: AdminCtx = Depends(operator), db: Session = Depends(get_db), _rl=Depends(rate_limit("admin_job_action", 30))):
    job = _job_or_404(db, job_id)
    if job.status == DocumentStatus.PROCESSING and ops.job_dto(job)["state"] != "STALE": raise HTTPException(409, "A job that is actively processing cannot be quarantined.")
    if job.quarantined_at: return {"id": str(job.id), "state": "QUARANTINED", "already_quarantined": True}
    res = db.execute(update(ProcessingJob).where(ProcessingJob.id == job.id, ProcessingJob.quarantined_at.is_(None)).values(status=DocumentStatus.FAILED, quarantined_at=datetime.now(timezone.utc), quarantine_reason=payload.reason, lease_expires_at=None))
    if res.rowcount != 1: db.rollback(); raise HTTPException(409, "The job changed state; refresh and retry.")
    doc = db.get(Document, job.document_id)
    if doc and doc.status not in (DocumentStatus.DELETED, DocumentStatus.DELETION_PENDING): doc.status = DocumentStatus.FAILED
    audit(db, ctx.user, "ADMIN_JOB_QUARANTINED", "processing_job", job.id, context={"reason": payload.reason}); db.commit()
    return {"id": str(job.id), "state": "QUARANTINED"}

@router.post("/jobs/{job_id}/release")
def release_job(job_id: UUID, ctx: AdminCtx = Depends(operator), db: Session = Depends(get_db)):
    job = _job_or_404(db, job_id)
    if not job.quarantined_at: raise HTTPException(409, "The job is not quarantined.")
    job.quarantined_at = None; job.quarantine_reason = None
    audit(db, ctx.user, "ADMIN_JOB_RELEASED", "processing_job", job.id); db.commit()
    return {"id": str(job.id), "state": ops.job_dto(job)["state"]}

# ---------------------------------------------------------------- AI ops / security / users / alerts
@router.get("/ai")
def ai_ops(ctx: AdminCtx = Depends(viewer)):
    return ops.ai_metrics()

@router.get("/security")
def security(window_hours: int = Query(24, ge=1, le=168), ctx: AdminCtx = Depends(viewer), db: Session = Depends(get_db)):
    since = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    by_type = dict(db.execute(select(SecurityEvent.event_type, func.count()).where(SecurityEvent.occurred_at >= since).group_by(SecurityEvent.event_type)).all())
    top_routes = db.execute(select(SecurityEvent.route, SecurityEvent.event_type, func.count().label("n")).where(SecurityEvent.occurred_at >= since).group_by(SecurityEvent.route, SecurityEvent.event_type).order_by(func.count().desc()).limit(10)).all()
    recent = db.scalars(select(SecurityEvent).order_by(SecurityEvent.occurred_at.desc()).limit(50)).all()
    share_actions = ["SHARE_CREATED", "SHARE_REVOKED", "ACCESS_GRANTED", "ACCESS_REVOKED", "FAMILY_ACCESS_GRANTED", "FAMILY_ACCESS_REVOKED"]
    audit_counts = dict(db.execute(select(AuditLog.action, func.count()).where(AuditLog.occurred_at >= since, AuditLog.action.in_(share_actions)).group_by(AuditLog.action)).all())
    admin_actions = db.scalars(select(AuditLog).where(AuditLog.action.like("ADMIN_%")).order_by(AuditLog.occurred_at.desc()).limit(25)).all()
    sharing_recent = db.scalars(select(AuditLog).where(AuditLog.action.in_(share_actions)).order_by(AuditLog.occurred_at.desc()).limit(25)).all()
    return {"window_hours": window_hours, "counts_by_type": by_type, "top_routes": [{"route": r, "event_type": t, "count": n} for r, t, n in top_routes], "recent_events": [ops.security_event_dto(e) for e in recent],
            "audit_counts": audit_counts, "recent_sharing_audit": [ops.audit_dto(r) for r in sharing_recent], "recent_admin_audit": [ops.audit_dto(r) for r in admin_actions],
            "note": "Events carry route templates, status codes and hashed client/account references only."}

@router.get("/users")
def users(page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100), q: str | None = Query(None, max_length=40, pattern=r"^[A-Za-z0-9_.-]*$"), ctx: AdminCtx = Depends(viewer), db: Session = Depends(get_db)):
    stmt = select(User)
    if q: stmt = stmt.where(User.username.ilike(f"{escape_like(q)}%", escape="\\"))
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.scalars(stmt.order_by(User.created_at.desc(), User.id).offset((page - 1) * page_size).limit(page_size)).all()
    counts = dict(db.execute(select(PatientProfile.owner_user_id, func.count()).where(PatientProfile.owner_user_id.in_([u.id for u in rows])).group_by(PatientProfile.owner_user_id)).all()) if rows else {}
    admins = {g.user_id for g in db.scalars(select(AdminGrant).where(AdminGrant.revoked_at.is_(None))).all()}
    audit(db, ctx.user, "ADMIN_USERS_VIEWED", "user_directory", None, context={"page": page, "filtered": bool(q)}); db.commit()
    return {"total": total, "page": page, "page_size": page_size, "items": [ops.user_dto(u, counts.get(u.id, 0), u.id in admins) for u in rows], "can_operate": ctx.can_operate,
            "note": "Operational account metadata only. Patient records are not accessible from the admin console."}

@router.post("/users/{user_id}/revoke-sessions")
def revoke_sessions(user_id: UUID, ctx: AdminCtx = Depends(operator), db: Session = Depends(get_db), _rl=Depends(rate_limit("admin_user_action", 20))):
    if user_id == ctx.user.id: raise HTTPException(409, "You cannot revoke your own sessions from the console.")
    target = db.get(User, user_id)
    if not target: raise HTTPException(404, "User not found.")
    target.session_version += 1
    audit(db, ctx.user, "ADMIN_USER_SESSIONS_REVOKED", "user", target.id); db.commit()
    return {"id": str(target.id), "session_version": target.session_version}

@router.get("/alerts")
def alerts(status: str | None = Query(None, pattern="^(OPEN|ACKNOWLEDGED|RESOLVED)$"), page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=100), ctx: AdminCtx = Depends(viewer), db: Session = Depends(get_db)):
    stmt = select(OpsAlert)
    if status: stmt = stmt.where(OpsAlert.status == status)
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.scalars(stmt.order_by(OpsAlert.last_seen_at.desc()).offset((page - 1) * page_size).limit(page_size)).all()
    return {"total": total, "items": [ops.alert_dto(a) for a in rows], "can_operate": ctx.can_operate,
            "delivery_note": "No external alert channel is configured in this build; delivery state is NOT_CONFIGURED and no notification is sent."}

@router.post("/alerts/evaluate")
def evaluate(ctx: AdminCtx = Depends(operator), db: Session = Depends(get_db), _rl=Depends(rate_limit("admin_alert_eval", 20))):
    result = ops.evaluate_alerts(db)
    audit(db, ctx.user, "ADMIN_ALERTS_EVALUATED", "ops_alert", None, context=result); db.commit()
    return result

def _alert_or_404(db, alert_id: UUID) -> OpsAlert:
    a = db.get(OpsAlert, alert_id)
    if not a: raise HTTPException(404, "Alert not found.")
    return a

@router.post("/alerts/{alert_id}/acknowledge")
def acknowledge(alert_id: UUID, ctx: AdminCtx = Depends(operator), db: Session = Depends(get_db)):
    a = _alert_or_404(db, alert_id)
    if a.status != "OPEN": raise HTTPException(409, "Only open alerts can be acknowledged.")
    a.status = "ACKNOWLEDGED"; a.acknowledged_by = ctx.user.id; a.acknowledged_at = datetime.now(timezone.utc)
    audit(db, ctx.user, "ADMIN_ALERT_ACKNOWLEDGED", "ops_alert", a.id); db.commit()
    return ops.alert_dto(a)

@router.post("/alerts/{alert_id}/resolve")
def resolve(alert_id: UUID, ctx: AdminCtx = Depends(operator), db: Session = Depends(get_db)):
    a = _alert_or_404(db, alert_id)
    if a.status == "RESOLVED": raise HTTPException(409, "The alert is already resolved.")
    a.status = "RESOLVED"; a.resolved_at = datetime.now(timezone.utc)
    audit(db, ctx.user, "ADMIN_ALERT_RESOLVED", "ops_alert", a.id); db.commit()
    return ops.alert_dto(a)
