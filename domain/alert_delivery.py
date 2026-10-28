"""Operational alert delivery.

Channels (all optional, configured by environment; nothing is simulated when none is set):
  * Webhook:  HEALTHGUARD_ALERT_WEBHOOK_URL (https required in production), optional HEALTHGUARD_ALERT_WEBHOOK_SECRET
              (HMAC-SHA256 over "<timestamp>.<body>", sent as X-HealthGuard-Signature / X-HealthGuard-Timestamp). Redirects are never followed.
  * Email:    HEALTHGUARD_ALERT_EMAIL_TO (comma separated) through the existing SMTP_* settings.
Policy: HEALTHGUARD_ALERT_MIN_SEVERITY (default WARNING), HEALTHGUARD_ALERT_MAX_ATTEMPTS (6), HEALTHGUARD_ALERT_REPEAT_HOURS (6, unacknowledged CRITICAL only),
HEALTHGUARD_ALERT_TIMEOUT_SECONDS (5).

Guarantees: each attempt is claimed with one atomic UPDATE, so concurrent workers never double-send; a sender that crashes mid-attempt is retried
after 5 minutes; failures back off exponentially; an alert that exhausts its attempts becomes EXHAUSTED and stays visible on the admin dashboard and in
metrics. An alert counts as DELIVERED when at least one channel accepted it (a failing channel is reported in metrics, not hidden).
Payloads contain alert text only (counts and component names): never patient data, emails, or job error text.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from db.models import OpsAlert
from domain.mailer import send_email, smtp_configured
from domain.observability import inc, set_gauge

logger = logging.getLogger("healthguard.alerts")
SEVERITY_RANK = {"INFO": 0, "WARNING": 1, "CRITICAL": 2}
SENDING_STALE_SECONDS = 300


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(d: datetime | None) -> datetime | None:
    return d if d is None or d.tzinfo else d.replace(tzinfo=timezone.utc)


def _int_env(name: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(os.getenv(name, str(default)))))
    except ValueError:
        return default


def webhook_url_ok(url: str) -> bool:
    try:
        u = urlparse(url or "")
    except ValueError:
        return False
    if u.scheme not in {"http", "https"} or not u.hostname:
        return False
    return u.scheme == "https" or os.getenv("APP_ENV", "development").lower() != "production"


def configured_channels() -> list[str]:
    channels = []
    if webhook_url_ok(os.getenv("HEALTHGUARD_ALERT_WEBHOOK_URL", "")): channels.append("webhook")
    if os.getenv("HEALTHGUARD_ALERT_EMAIL_TO", "").strip() and smtp_configured(): channels.append("email")
    return channels


def _min_rank() -> int:
    return SEVERITY_RANK.get(os.getenv("HEALTHGUARD_ALERT_MIN_SEVERITY", "WARNING").upper(), 1)


def payload_for(a: OpsAlert) -> dict:
    iso = lambda d: _aware(d).isoformat() if d else None
    return {"type": "healthguard.alert", "version": 1, "environment": os.getenv("APP_ENV", "development"), "fingerprint": a.fingerprint, "source": a.source,
            "severity": a.severity, "title": a.title, "detail": a.detail, "status": a.status, "occurrences": a.occurrences,
            "first_seen_at": iso(a.first_seen_at), "last_seen_at": iso(a.last_seen_at)}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):          # a redirect could point a webhook at an internal service
        return None


def _send_webhook(a: OpsAlert) -> None:
    url = os.getenv("HEALTHGUARD_ALERT_WEBHOOK_URL", "")
    body = json.dumps(payload_for(a), separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ts = str(int(time.time())); headers = {"Content-Type": "application/json", "User-Agent": "HealthGuard-Alerts/1", "X-HealthGuard-Event": "alert", "X-HealthGuard-Timestamp": ts}
    secret = os.getenv("HEALTHGUARD_ALERT_WEBHOOK_SECRET", "")
    if secret:
        headers["X-HealthGuard-Signature"] = "sha256=" + hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    opener = urllib.request.build_opener(_NoRedirect)
    with opener.open(urllib.request.Request(url, data=body, headers=headers, method="POST"), timeout=_int_env("HEALTHGUARD_ALERT_TIMEOUT_SECONDS", 5, 1, 30)) as r:
        if not 200 <= r.status < 300:
            raise RuntimeError(f"webhook returned HTTP {r.status}")


def _send_email(a: OpsAlert) -> None:
    to = [x.strip() for x in os.getenv("HEALTHGUARD_ALERT_EMAIL_TO", "").split(",") if x.strip()]
    p = payload_for(a)
    body = "\n".join([f"{p['severity']}: {p['title']}", f"Source: {p['source']}   Environment: {p['environment']}   Alert: {p['fingerprint']}", f"Occurrences: {p['occurrences']}",
                      f"First seen: {p['first_seen_at']}   Last seen: {p['last_seen_at']}"] + ([f"Detail: {p['detail']}"] if p["detail"] else []) +
                     ["", "Open the HealthGuard admin console (Operations > Alerts) to acknowledge or investigate."])
    send_email(to=to, subject=f"[HealthGuard {a.severity}] {a.title}"[:200], body=body)    # shared SMTP sender (same settings as verification/reminders)


_SENDERS = {"webhook": _send_webhook, "email": _send_email}


def _due(a: OpsAlert, now: datetime, max_attempts: int, repeat: timedelta) -> bool:
    last = _aware(a.last_delivery_at)
    if a.status != "OPEN":
        return False
    if a.delivery_state in {"NOT_CONFIGURED", "PENDING"}: return True
    if a.delivery_state == "FAILED":
        return last is None or now >= last + timedelta(seconds=min(30 * 2 ** max(a.delivery_attempts - 1, 0), 1800))
    if a.delivery_state == "SENDING": return last is None or now >= last + timedelta(seconds=SENDING_STALE_SECONDS)
    if a.delivery_state == "DELIVERED": return a.severity == "CRITICAL" and last is not None and now >= last + repeat
    return False


def deliver_pending_alerts(db: Session, now: datetime | None = None) -> dict:
    now = now or _now(); channels = configured_channels()
    out = {"channels": channels, "attempted": 0, "delivered": 0, "failed": 0, "exhausted": 0, "channel_failures": {}}
    if not channels:
        return out
    max_attempts = _int_env("HEALTHGUARD_ALERT_MAX_ATTEMPTS", 6, 1, 20); repeat = timedelta(hours=_int_env("HEALTHGUARD_ALERT_REPEAT_HOURS", 6, 1, 168))
    for a in db.scalars(select(OpsAlert).where(OpsAlert.status.in_(["OPEN"])).order_by(OpsAlert.first_seen_at)).all():
        if SEVERITY_RANK.get(a.severity, 0) < _min_rank():
            if a.delivery_state != "BELOW_THRESHOLD":
                a.delivery_state = "BELOW_THRESHOLD"; db.commit()
            continue
        if not _due(a, now, max_attempts, repeat):
            continue
        repeating = a.delivery_state == "DELIVERED"
        prior_state, prior_attempts = a.delivery_state, a.delivery_attempts
        attempts = 1 if repeating else prior_attempts + 1
        claimed = db.execute(update(OpsAlert).where(OpsAlert.id == a.id, OpsAlert.delivery_state == prior_state, OpsAlert.delivery_attempts == prior_attempts)
                             .values(delivery_state="SENDING", delivery_attempts=attempts, last_delivery_at=now))
        db.commit()
        if claimed.rowcount != 1:
            continue                                        # another worker claimed this attempt
        db.refresh(a); out["attempted"] += 1; ok = 0
        for ch in channels:
            try:
                _SENDERS[ch](a); ok += 1; inc("healthguard_alert_delivery_total", channel=ch, outcome="delivered")
            except Exception as exc:                        # never log the URL (may contain a token) or the payload
                out["channel_failures"][ch] = out["channel_failures"].get(ch, 0) + 1
                inc("healthguard_alert_delivery_total", channel=ch, outcome="failed"); logger.warning("alert delivery failed channel=%s error_type=%s", ch, type(exc).__name__)
        if ok:
            a.delivery_state = "DELIVERED"; out["delivered"] += 1
        elif attempts >= max_attempts:
            a.delivery_state = "EXHAUSTED"; out["exhausted"] += 1; out["failed"] += 1
            for ch in channels: inc("healthguard_alert_delivery_total", channel=ch, outcome="exhausted")
        else:
            a.delivery_state = "FAILED"; out["failed"] += 1
        db.commit()
    _record_backlog(db, now)
    return out


def _record_backlog(db: Session, now: datetime) -> None:
    stuck = [a for a in db.scalars(select(OpsAlert).where(OpsAlert.status == "OPEN")).all() if a.delivery_state in {"FAILED", "EXHAUSTED", "SENDING", "PENDING"}]
    set_gauge("healthguard_alerts_undelivered", len(stuck), scope="all")
    set_gauge("healthguard_alert_oldest_undelivered_age_seconds", max([(now - _aware(a.first_seen_at)).total_seconds() for a in stuck], default=0), scope="all")


def _cycle_lock(seconds: int) -> bool:
    url = os.getenv("REDIS_URL", "")
    if not url:
        return True
    try:
        from domain.rate_limit import _redis_client
        return bool(_redis_client(url).set("healthguard:alert-cycle", "1", nx=True, ex=max(5, seconds)))
    except Exception:
        return True                                         # Redis trouble must not silence alerting; the per-attempt DB claim still prevents double-sends


def run_alert_cycle(db: Session) -> dict:
    """Evaluate real conditions, then deliver. Safe to call from every worker: a short Redis lock avoids duplicate evaluation, and the DB claim avoids duplicate sends."""
    from domain.admin_ops import evaluate_alerts
    if not _cycle_lock(_int_env("HEALTHGUARD_ALERT_CYCLE_SECONDS", 60, 10, 3600) - 1):
        return {"skipped": "another worker is running the alert cycle"}
    evaluation = evaluate_alerts(db)
    return {"evaluation": evaluation, "delivery": deliver_pending_alerts(db)}
