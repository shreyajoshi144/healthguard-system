"""Email reminders for confirmed / scheduled follow-ups.

Guarantees
* Only EXACT-dated, user-confirmed (or user-created) open follow-ups are ever reminded. Suggestions awaiting
  confirmation, approximate/year/unknown dates, completed and cancelled actions never produce an email.
* Authorization is re-checked at SEND time (revoked/expired access, deactivated or unverified account, deleted
  patient => the delivery is cancelled, not sent).
* Planning is idempotent (unique key user+action+lead+due_date). Sending is at-least-once with a deterministic
  Message-ID; the lease/claim is an atomic UPDATE so concurrent workers cannot both send the same delivery
  except after a crash between SMTP success and the status write (documented, detectable by Message-ID).
* Retries use exponential backoff; permanent failures stop immediately; stale reminders expire instead of
  arriving late. Emails contain no health details unless the user opted in to including the follow-up title.
"""
from __future__ import annotations
import logging, os, re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from sqlalchemy import select, update, or_, and_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from db.models import (AccountStatus, AuditLog, DatePrecision, HealthAction, HealthActionStatus, PatientAccess, PatientProfile,
                       ReminderDelivery, ReminderPreference, User, AccessGrantStatus)
from domain.consumer import awaiting_confirmation
from domain.mailer import EmailDeliveryError, send_email
from domain.observability import inc

logger = logging.getLogger(__name__)
ALLOWED_LEADS = (0, 1, 2, 3, 7, 14)
MAX_LEADS = 4
MAX_ATTEMPTS = int(os.getenv("HEALTHGUARD_REMINDER_MAX_ATTEMPTS", "5"))
BACKOFF_BASE_SECONDS = int(os.getenv("HEALTHGUARD_REMINDER_BACKOFF_SECONDS", "60"))
BACKOFF_CAP_SECONDS = 3600
LEASE_SECONDS = int(os.getenv("HEALTHGUARD_REMINDER_LEASE_SECONDS", "120"))
MAX_LATENESS = timedelta(hours=int(os.getenv("HEALTHGUARD_REMINDER_MAX_LATENESS_HOURS", "12")))
BATCH = int(os.getenv("HEALTHGUARD_REMINDER_BATCH", "50"))
TERMINAL = {"SENT", "FAILED", "EXPIRED", "CANCELLED", "SKIPPED"}


def _utc(d: datetime | None) -> datetime | None:
    return d if d is None or d.tzinfo else d.replace(tzinfo=timezone.utc)

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def valid_timezone(name: str) -> bool:
    try:
        ZoneInfo(name); return True
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False

def backoff_seconds(attempts: int) -> int:
    return min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * (2 ** max(0, attempts - 1)))

def schedule_time(due: date, lead_days: int, send_hour: int, tz: str) -> datetime:
    local = datetime.combine(due - timedelta(days=lead_days), time(send_hour), tzinfo=ZoneInfo(tz))
    return local.astimezone(timezone.utc)

def eligible_action(a: HealthAction | None) -> bool:
    return bool(a and a.status in (HealthActionStatus.PLANNED, HealthActionStatus.SCHEDULED) and a.due_date is not None
                and a.temporal_precision == DatePrecision.EXACT and not awaiting_confirmation(a))

def subscriber_authorized(db: Session, pref: ReminderPreference, now: datetime | None = None) -> bool:
    now = now or now_utc()
    user = db.get(User, pref.user_id); patient = db.get(PatientProfile, pref.patient_id)
    if not user or not patient or user.status != AccountStatus.ACTIVE or not user.email_verified or user.deletion_pending or getattr(patient, "deletion_pending", False):
        return False
    if patient.owner_user_id == user.id:
        return True
    g = db.scalar(select(PatientAccess).where(PatientAccess.patient_id == pref.patient_id, PatientAccess.user_id == pref.user_id, PatientAccess.revoked_at.is_(None), PatientAccess.status == AccessGrantStatus.ACTIVE))
    if not g or (g.expires_at is not None and _utc(g.expires_at) <= now):
        return False
    return "HEALTH_ACTIONS" in (g.scopes or [])

def _audit(db: Session, action: str, delivery: ReminderDelivery, **ctx) -> None:
    db.add(AuditLog(actor_user_id=None, action=action, resource_type="reminder_delivery", resource_id=str(delivery.id), context={"lead_days": delivery.lead_days, "attempts": delivery.attempts, **ctx}))

def _set(delivery: ReminderDelivery, status: str, **kw) -> None:
    delivery.status = status; delivery.lease_expires_at = None
    for k, v in kw.items(): setattr(delivery, k, v)

# ------------------------------------------------------------------ planning
def plan_for_pref(db: Session, pref: ReminderPreference, now: datetime | None = None) -> int:
    """Create/refresh PENDING deliveries for one preference. Idempotent; returns number created or revived."""
    now = now or now_utc(); changed = 0
    if not pref.enabled or not subscriber_authorized(db, pref, now):
        return 0
    today = now.date()
    actions = db.scalars(select(HealthAction).where(HealthAction.patient_id == pref.patient_id, HealthAction.due_date.is_not(None),
                                                    HealthAction.due_date >= today - timedelta(days=1), HealthAction.due_date <= today + timedelta(days=max(ALLOWED_LEADS) + 2))).all()
    for a in actions:
        if not eligible_action(a):
            continue
        for lead in sorted({int(x) for x in pref.lead_days or []}):
            when = schedule_time(a.due_date, lead, pref.send_hour, pref.timezone)
            if when + MAX_LATENESS < now:
                continue                                           # that moment is long past: never send a late reminder
            row = db.scalar(select(ReminderDelivery).where(ReminderDelivery.user_id == pref.user_id, ReminderDelivery.action_id == a.id, ReminderDelivery.lead_days == lead, ReminderDelivery.due_date == a.due_date))
            if row is None:
                try:
                    with db.begin_nested():
                        db.add(ReminderDelivery(user_id=pref.user_id, patient_id=pref.patient_id, action_id=a.id, lead_days=lead, due_date=a.due_date, scheduled_for=when, status="PENDING", attempts=0))
                    changed += 1
                except IntegrityError:
                    pass                                           # a concurrent planner created it
            elif row.status in ("PENDING", "CANCELLED") and (row.status == "CANCELLED" or _utc(row.scheduled_for) != when):
                row.scheduled_for = when; row.status = "PENDING"; row.attempts = 0; row.next_attempt_at = None; row.last_error = None; changed += 1
    return changed

def cancel_stale(db: Session, now: datetime | None = None, limit: int = 500) -> int:
    """Cancel PENDING deliveries whose action/preference/authorization no longer justifies sending."""
    n = 0; now = now or now_utc()
    rows = db.scalars(select(ReminderDelivery).where(ReminderDelivery.status == "PENDING").order_by(ReminderDelivery.scheduled_for).limit(limit)).all()
    for d in rows:
        pref = db.scalar(select(ReminderPreference).where(ReminderPreference.user_id == d.user_id, ReminderPreference.patient_id == d.patient_id))
        a = db.get(HealthAction, d.action_id)
        ok = pref and pref.enabled and d.lead_days in (pref.lead_days or []) and eligible_action(a) and a.due_date == d.due_date and a.patient_id == d.patient_id and subscriber_authorized(db, pref, now)
        if not ok:
            _set(d, "CANCELLED", last_error=None); n += 1
    return n

def reconcile(db: Session, now: datetime | None = None) -> dict:
    now = now or now_utc(); created = 0
    for pref in db.scalars(select(ReminderPreference).where(ReminderPreference.enabled.is_(True))).all():
        created += plan_for_pref(db, pref, now)
    cancelled = cancel_stale(db, now)
    db.commit()
    return {"planned": created, "cancelled": cancelled}

# ------------------------------------------------------------------ dispatch
def recover_leases(db: Session, now: datetime | None = None) -> int:
    now = now or now_utc(); n = 0
    for d in db.scalars(select(ReminderDelivery).where(ReminderDelivery.status == "SENDING", ReminderDelivery.lease_expires_at < now)).all():
        if d.attempts >= MAX_ATTEMPTS:
            _set(d, "FAILED", last_error="lease_expired"); _audit(db, "REMINDER_FAILED", d, code="lease_expired")
        else:
            _set(d, "PENDING", next_attempt_at=now)
        n += 1
    db.commit(); return n

def claim_due(db: Session, now: datetime | None = None, limit: int = BATCH) -> list:
    """Atomically claim due deliveries. Two workers can never claim the same row."""
    now = now or now_utc(); claimed = []
    ids = db.scalars(select(ReminderDelivery.id).where(ReminderDelivery.status == "PENDING", ReminderDelivery.scheduled_for <= now,
                                                       or_(ReminderDelivery.next_attempt_at.is_(None), ReminderDelivery.next_attempt_at <= now)).order_by(ReminderDelivery.scheduled_for).limit(limit)).all()
    for did in ids:
        res = db.execute(update(ReminderDelivery).where(ReminderDelivery.id == did, ReminderDelivery.status == "PENDING")
                         .values(status="SENDING", attempts=ReminderDelivery.attempts + 1, lease_expires_at=now + timedelta(seconds=LEASE_SECONDS)))
        db.commit()
        if res.rowcount == 1:
            claimed.append(did)
    return claimed

_CTRL = re.compile(r"[\x00-\x1f\x7f]+")

def build_message(db: Session, d: ReminderDelivery, pref: ReminderPreference, action: HealthAction) -> tuple[str, str, str]:
    from domain.email_verification import public_base_url
    base = public_base_url()
    host = re.sub(r"^https?://", "", base).split("/")[0] or "healthguard.local"
    days = (d.due_date - now_utc().date()).days
    when = "today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days" if days > 1 else f"on {d.due_date.isoformat()}"
    link = f"{base}/hub?patient_id={d.patient_id}#/followups"
    lines = [f"You have a HealthGuard follow-up reminder due {when} ({d.due_date.isoformat()}).", ""]
    if pref.include_title:
        lines += [f"Follow-up: {_CTRL.sub(' ', action.title)[:120]}", ""]
    lines += [f"Review it here: {link}", "", "You are receiving this because you turned on email reminders. You can change or turn them off on that page."]
    if not pref.include_title:
        lines.append("This message intentionally contains no health details.")
    return "HealthGuard follow-up reminder", "\n".join(lines), f"<reminder-{d.id}@{host}>"

def deliver(db: Session, delivery_id, sender=send_email, now: datetime | None = None) -> str:
    """Revalidate and send one claimed delivery. Returns the resulting status."""
    now = now or now_utc()
    d = db.get(ReminderDelivery, delivery_id)
    if d is None or d.status != "SENDING":
        return "SKIPPED"
    pref = db.scalar(select(ReminderPreference).where(ReminderPreference.user_id == d.user_id, ReminderPreference.patient_id == d.patient_id))
    action = db.get(HealthAction, d.action_id)
    if not (pref and pref.enabled and d.lead_days in (pref.lead_days or []) and eligible_action(action) and action.due_date == d.due_date and subscriber_authorized(db, pref, now)):
        _set(d, "CANCELLED"); db.commit(); inc("healthguard_reminders_total", outcome="cancelled"); return "CANCELLED"
    if _utc(d.scheduled_for) + MAX_LATENESS < now or d.due_date < now.date() - timedelta(days=1):
        _set(d, "EXPIRED"); db.commit(); inc("healthguard_reminders_total", outcome="expired"); return "EXPIRED"
    user = db.get(User, d.user_id)
    subject, body, mid = build_message(db, d, pref, action)
    try:
        sender(to=user.email, subject=subject, body=body, message_id=mid)
    except EmailDeliveryError as exc:
        if exc.permanent or d.attempts >= MAX_ATTEMPTS:
            _set(d, "FAILED", last_error=exc.code); _audit(db, "REMINDER_FAILED", d, code=exc.code, permanent=exc.permanent)
            outcome = "failed"
        else:
            _set(d, "PENDING", last_error=exc.code, next_attempt_at=now + timedelta(seconds=backoff_seconds(d.attempts))); outcome = "retry"
        db.commit(); inc("healthguard_reminders_total", outcome=outcome); return d.status
    except Exception as exc:                                       # unexpected: treat as transient, never crash the worker
        logger.warning("reminder_send_unexpected_error error_type=%s", type(exc).__name__)
        if d.attempts >= MAX_ATTEMPTS:
            _set(d, "FAILED", last_error="unexpected_error"); _audit(db, "REMINDER_FAILED", d, code="unexpected_error")
        else:
            _set(d, "PENDING", last_error="unexpected_error", next_attempt_at=now + timedelta(seconds=backoff_seconds(d.attempts)))
        db.commit(); inc("healthguard_reminders_total", outcome="retry"); return d.status
    _set(d, "SENT", sent_at=now, last_error=None); _audit(db, "REMINDER_SENT", d); db.commit()
    inc("healthguard_reminders_total", outcome="sent"); return "SENT"

_last_reconcile = 0.0

def run_tick(session_factory, sender=send_email, now: datetime | None = None, force_reconcile: bool = False) -> dict:
    """One dispatcher pass: reconcile (throttled), recover expired leases, claim and deliver due reminders."""
    import time as _t
    global _last_reconcile
    db = session_factory(); out = {"planned": 0, "cancelled": 0, "recovered": 0, "sent": 0, "retry": 0, "failed": 0, "other": 0}
    try:
        if force_reconcile or _t.monotonic() - _last_reconcile >= float(os.getenv("HEALTHGUARD_REMINDER_RECONCILE_SECONDS", "60")):
            r = reconcile(db, now); out.update(r); _last_reconcile = _t.monotonic()
        out["recovered"] = recover_leases(db, now)
        for did in claim_due(db, now):
            res = deliver(db, did, sender, now)
            key = {"SENT": "sent", "PENDING": "retry", "FAILED": "failed"}.get(res, "other"); out[key] += 1
        return out
    finally:
        db.close()
