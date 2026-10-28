"""Email reminder preferences and delivery status (per user, per patient). Email only; no SMS/push."""
from __future__ import annotations
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session
from backend.auth import get_current_user
from db.models import AccessScope, HealthAction, ReminderDelivery, ReminderPreference, User
from db.session import get_db
from domain.audit import audit
from domain.authorization import require_scope
from domain.mailer import smtp_configured
from domain import reminders as R
from domain.rate_limit import rate_limit

router = APIRouter(prefix="/api")

class PrefIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    lead_days: list[int] = Field(default_factory=list, max_length=R.MAX_LEADS)
    send_hour: int = Field(default=9, ge=0, le=23)
    timezone: str = Field(default="UTC", min_length=1, max_length=64)
    include_title: bool = False

def _pref_out(pref: ReminderPreference | None, user: User) -> dict:
    return {"enabled": bool(pref and pref.enabled), "lead_days": sorted(pref.lead_days) if pref else [1], "send_hour": pref.send_hour if pref else 9,
            "timezone": pref.timezone if pref else "UTC", "include_title": bool(pref and pref.include_title),
            "allowed_lead_days": list(R.ALLOWED_LEADS), "max_lead_days": R.MAX_LEADS, "email_verified": bool(user.email_verified), "email_delivery_configured": smtp_configured(),
            "channel": "EMAIL"}

@router.get("/patients/{patient_id}/reminder-preferences")
def get_prefs(patient_id: UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    require_scope(db, user, patient_id, AccessScope.HEALTH_ACTIONS)
    pref = db.scalar(select(ReminderPreference).where(ReminderPreference.user_id == user.id, ReminderPreference.patient_id == patient_id))
    return _pref_out(pref, user)

@router.put("/patients/{patient_id}/reminder-preferences")
def put_prefs(patient_id: UUID, payload: PrefIn, db: Session = Depends(get_db), user: User = Depends(get_current_user), _rl=Depends(rate_limit("reminder_prefs", 30))):
    require_scope(db, user, patient_id, AccessScope.HEALTH_ACTIONS)                 # read access is enough to be reminded; nothing is written to the record
    leads = sorted(set(payload.lead_days))
    if any(x not in R.ALLOWED_LEADS for x in leads):
        raise HTTPException(422, f"Lead days must be chosen from {list(R.ALLOWED_LEADS)}.")
    if not R.valid_timezone(payload.timezone):
        raise HTTPException(422, "Unknown time zone.")
    if payload.enabled and not leads:
        raise HTTPException(422, "Choose at least one reminder time.")
    if payload.enabled and not user.email_verified:
        raise HTTPException(409, "Verify your email address before turning on reminders.")
    pref = db.scalar(select(ReminderPreference).where(ReminderPreference.user_id == user.id, ReminderPreference.patient_id == patient_id))
    if pref is None:
        pref = ReminderPreference(user_id=user.id, patient_id=patient_id); db.add(pref)
    pref.enabled, pref.lead_days, pref.send_hour, pref.timezone, pref.include_title = payload.enabled, leads, payload.send_hour, payload.timezone, payload.include_title
    db.flush()
    if not payload.enabled:
        db.execute(update(ReminderDelivery).where(ReminderDelivery.user_id == user.id, ReminderDelivery.patient_id == patient_id, ReminderDelivery.status == "PENDING").values(status="CANCELLED"))
    else:
        R.cancel_stale(db); R.plan_for_pref(db, pref)                                # takes effect immediately; the worker reconciles continuously
    audit(db, user, "REMINDER_PREFERENCES_UPDATED", "patient", patient_id, context={"enabled": payload.enabled, "lead_count": len(leads)})
    db.commit(); db.refresh(pref)
    return _pref_out(pref, user)

@router.get("/patients/{patient_id}/reminder-deliveries")
def deliveries(patient_id: UUID, status: str | None = Query(None, pattern="^(PENDING|SENDING|SENT|FAILED|CANCELLED|EXPIRED|SKIPPED)$"), page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
               db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    require_scope(db, user, patient_id, AccessScope.HEALTH_ACTIONS)
    stmt = select(ReminderDelivery).where(ReminderDelivery.user_id == user.id, ReminderDelivery.patient_id == patient_id)   # only the caller's own deliveries
    if status: stmt = stmt.where(ReminderDelivery.status == status)
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.scalars(stmt.order_by(ReminderDelivery.scheduled_for.desc(), ReminderDelivery.id).offset((page - 1) * page_size).limit(page_size)).all()
    titles = {a.id: a.title for a in db.scalars(select(HealthAction).where(HealthAction.id.in_([r.action_id for r in rows]))).all()} if rows else {}
    return {"total": total, "page": page, "page_size": page_size, "items": [
        {"id": str(r.id), "action_id": str(r.action_id), "action_title": titles.get(r.action_id), "lead_days": r.lead_days, "due_date": r.due_date, "scheduled_for": r.scheduled_for, "status": r.status,
         "attempts": r.attempts, "next_attempt_at": r.next_attempt_at, "last_error": r.last_error, "sent_at": r.sent_at} for r in rows]}
