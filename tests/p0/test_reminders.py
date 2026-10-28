"""Email reminders for confirmed/scheduled follow-ups: preferences, planning, idempotency, dispatch over a real SMTP
protocol capture server, retries/backoff, cancellation/expiry, send-time authorization, multi-worker claims, PHI minimization, audit."""
from __future__ import annotations
import email, json, re, socketserver, threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from db.models import *
from domain import reminders as R, admin_ops as ops
from domain.mailer import EmailDeliveryError, send_email
from tests.test_sprint5_phase1_security import make_db, seed, client, grant, clean

TITLE = "Repeat HbA1c blood test"
UTC = timezone.utc

# ---------------- a real SMTP server (protocol-level) with controllable behaviour ----------------
class _H(socketserver.StreamRequestHandler):
    def handle(self):
        srv = self.server; w = self.wfile.write; w(b"220 test ESMTP\r\n"); data = None
        while True:
            line = self.rfile.readline()
            if not line: return
            cmd = line.decode(errors="replace").strip(); up = cmd.upper()
            if data is not None:
                if cmd == ".":
                    if srv.mode == "tempfail": w(b"451 try again later\r\n")
                    else: srv.messages.append("\n".join(data)); w(b"250 queued\r\n")
                    data = None
                else: data.append(cmd[1:] if cmd.startswith("..") else cmd)
                continue
            if up.startswith(("EHLO", "HELO")): w(b"250-test\r\n250 OK\r\n")
            elif up.startswith("MAIL"): w(b"250 OK\r\n")
            elif up.startswith("RCPT"): w(b"550 no such user\r\n" if srv.mode == "reject" else b"250 OK\r\n")
            elif up == "DATA": w(b"354 go\r\n"); data = []
            elif up == "QUIT": w(b"221 bye\r\n"); return
            else: w(b"250 OK\r\n")

class FakeSMTP(socketserver.ThreadingTCPServer):
    allow_reuse_address = True; daemon_threads = True
    def __init__(self): super().__init__(("127.0.0.1", 0), _H); self.messages: list[str] = []; self.mode = "ok"

@pytest.fixture
def smtp(monkeypatch):
    s = FakeSMTP(); threading.Thread(target=s.serve_forever, daemon=True).start()
    monkeypatch.setenv("SMTP_HOST", "127.0.0.1"); monkeypatch.setenv("SMTP_PORT", str(s.server_address[1])); monkeypatch.setenv("SMTP_FROM", "healthguard@example.com"); monkeypatch.setenv("SMTP_STARTTLS", "false")
    monkeypatch.setenv("HEALTHGUARD_PUBLIC_BASE_URL", "https://app.example.com")
    yield s
    s.shutdown(); s.server_close()

@pytest.fixture
def env():
    from domain import rate_limit as rl
    rl._local.clear()
    S = make_db(); db, a, b, c, p, q = seed(S)
    p.first_name = "Zelda"; db.commit()
    yield S, db, a, b, c, p, q
    clean(); rl._local.clear()

def parsed(raw: str):
    m = email.message_from_string(raw); return m, m.get_payload(decode=True).decode()

def mk_action(db, p, *, title=TITLE, due=None, status=HealthActionStatus.SCHEDULED, source=HealthActionSource.USER_CREATED, precision=DatePrecision.EXACT, confirmed=True):
    a = HealthAction(patient_id=p.id, title=title, action_type="FOLLOW_UP", source=source, provenance=Provenance.USER_REPORTED, status=status,
                     due_date=due, temporal_precision=precision, confirmed_at=datetime.now(UTC) if (confirmed and source == HealthActionSource.DOCUMENT_DERIVED) else None)
    db.add(a); db.commit(); return a

def set_prefs(db, user, p, **kw):
    body = {"enabled": True, "lead_days": [1], "send_hour": 9, "timezone": "UTC", "include_title": False, **kw}
    return client(db, user).put(f"/api/patients/{p.id}/reminder-preferences", json=body)

def due_in(days): return date.today() + timedelta(days=days)
def sched(a_due, lead=1, hour=9, tz="UTC"): return R.schedule_time(a_due, lead, hour, tz)
def deliveries(db, **kw): 
    db.expire_all(); return db.scalars(select(ReminderDelivery).filter_by(**kw).order_by(ReminderDelivery.lead_days)).all()
def tick(S, when, **kw): return R.run_tick(S, now=when, force_reconcile=True, **kw)

# ---------------- preferences: validation + authorization ----------------
def test_preferences_defaults_validation_and_audit(env):
    S, db, a, b, c, p, q = env
    g = client(db, a).get(f"/api/patients/{p.id}/reminder-preferences").json()
    assert g["enabled"] is False and g["channel"] == "EMAIL" and g["allowed_lead_days"] == [0, 1, 2, 3, 7, 14] and g["email_verified"] is True
    url = f"/api/patients/{p.id}/reminder-preferences"; base = {"enabled": True, "lead_days": [1], "send_hour": 9, "timezone": "UTC", "include_title": False}
    u = lambda **kw: client(db, a).put(url, json={**base, **kw})
    assert u(lead_days=[5]).status_code == 422 and u(lead_days=[]).status_code == 422 and u(lead_days=[0, 1, 2, 3, 7]).status_code == 422
    assert u(timezone="Mars/Olympus").status_code == 422 and u(send_hour=24).status_code == 422 and u(send_hour=-1).status_code == 422
    assert u(sms=True).status_code == 422                                                       # no other channels exist
    assert u(enabled=False, lead_days=[]).status_code == 200
    ok = u(lead_days=[7, 1, 1, 0], send_hour=7, timezone="Asia/Kolkata", include_title=True).json()
    assert ok["lead_days"] == [0, 1, 7] and ok["timezone"] == "Asia/Kolkata" and ok["include_title"] is True
    row = db.scalar(select(AuditLog).where(AuditLog.action == "REMINDER_PREFERENCES_UPDATED").order_by(AuditLog.occurred_at.desc()))
    assert row.context == {"enabled": True, "lead_count": 3}

def test_unverified_email_cannot_enable(env):
    S, db, a, b, c, p, q = env
    a.email_verified = False; db.commit()
    assert set_prefs(db, a, p).status_code == 409
    assert set_prefs(db, a, p, enabled=False, lead_days=[]).status_code == 200

def test_preference_authorization_and_isolation(env):
    S, db, a, b, c, p, q = env
    assert set_prefs(db, c, p).status_code == 403 and client(db, c).get(f"/api/patients/{p.id}/reminder-preferences").status_code == 403
    assert set_prefs(db, b, p).status_code == 403                                               # owns another patient only
    grant(db, a, p, b, ["PROFILE", "MEDICATIONS"])                                              # no HEALTH_ACTIONS
    assert set_prefs(db, b, p).status_code == 403
    g = db.scalar(select(PatientAccess).where(PatientAccess.user_id == b.id)); g.scopes = ["HEALTH_ACTIONS"]; db.commit()
    assert set_prefs(db, b, p).status_code == 200                                               # read access suffices; users manage only their own preference
    assert client(db, a).get(f"/api/patients/{p.id}/reminder-preferences").json()["enabled"] is False
    assert client(db, b).get(f"/api/patients/{p.id}/reminder-preferences").json()["enabled"] is True

# ---------------- planning ----------------
def test_planning_schedule_idempotency_and_eligibility(env):
    S, db, a, b, c, p, q = env
    due = due_in(10); good = mk_action(db, p, due=due)
    mk_action(db, p, title="suggested", due=due, source=HealthActionSource.DOCUMENT_DERIVED, status=HealthActionStatus.PLANNED, confirmed=False)     # unconfirmed suggestion
    mk_action(db, p, title="approx", due=due, precision=DatePrecision.APPROXIMATE); mk_action(db, p, title="year", due=due, precision=DatePrecision.YEAR)
    mk_action(db, p, title="done", due=due, status=HealthActionStatus.COMPLETED); mk_action(db, p, title="cancelled", due=due, status=HealthActionStatus.CANCELLED)
    mk_action(db, p, title="nodate", due=None, status=HealthActionStatus.PLANNED)
    confirmed_doc = mk_action(db, p, title="confirmed-suggestion", due=due, source=HealthActionSource.DOCUMENT_DERIVED, status=HealthActionStatus.SCHEDULED, confirmed=True)
    assert set_prefs(db, a, p, lead_days=[0, 1, 7], send_hour=9, timezone="Asia/Kolkata").status_code == 200
    rows = deliveries(db)
    assert {r.action_id for r in rows} == {good.id, confirmed_doc.id} and len(rows) == 6
    r1 = next(r for r in rows if r.action_id == good.id and r.lead_days == 1)
    assert r1.scheduled_for.replace(tzinfo=UTC) == datetime.combine(due - timedelta(days=1), datetime.min.time(), tzinfo=UTC) + timedelta(hours=3, minutes=30) and r1.status == "PENDING"
    for _ in range(3): R.reconcile(db); set_prefs(db, a, p, lead_days=[0, 1, 7], send_hour=9, timezone="Asia/Kolkata")
    assert len(deliveries(db)) == 6                                                              # idempotent
    # confirming a suggestion later makes it eligible
    sug = db.scalar(select(HealthAction).where(HealthAction.title == "suggested"))
    assert client(db, a).post(f"/api/health-actions/{sug.id}/confirm").status_code == 200
    R.reconcile(db); assert len(deliveries(db)) == 9

def test_stale_past_reminders_are_not_planned(env):
    S, db, a, b, c, p, q = env
    mk_action(db, p, due=due_in(3)); assert set_prefs(db, a, p, lead_days=[0, 1, 7, 14]).status_code == 200
    assert sorted(r.lead_days for r in deliveries(db)) == [0, 1]                                  # 7- and 14-day leads already passed >12h ago

def test_timezone_and_dst_scheduling():
    assert R.schedule_time(date(2026, 3, 9), 1, 9, "America/New_York") == datetime(2026, 3, 8, 13, 0, tzinfo=UTC)    # DST starts Mar 8 2026 -> EDT (UTC-4)
    assert R.schedule_time(date(2026, 3, 5), 1, 9, "America/New_York") == datetime(2026, 3, 4, 14, 0, tzinfo=UTC)    # EST (UTC-5)
    assert R.schedule_time(date(2026, 6, 1), 0, 9, "Asia/Kolkata") == datetime(2026, 6, 1, 3, 30, tzinfo=UTC)
    assert R.backoff_seconds(1) == 60 and R.backoff_seconds(2) == 120 and R.backoff_seconds(4) == 480 and R.backoff_seconds(30) == 3600

# ---------------- dispatch over real SMTP ----------------
def test_send_minimal_phi_email_once_with_audit(env, smtp):
    S, db, a, b, c, p, q = env
    due = due_in(2); act = mk_action(db, p, due=due); set_prefs(db, a, p, lead_days=[1])
    d = deliveries(db)[0]; when = d.scheduled_for.replace(tzinfo=UTC) + timedelta(minutes=1)
    assert tick(S, when - timedelta(hours=1))["sent"] == 0 and not smtp.messages                # not due yet
    out = tick(S, when); assert out["sent"] == 1 and len(smtp.messages) == 1
    m, body = parsed(smtp.messages[0]); low = body.lower()
    assert m["Subject"] == "HealthGuard follow-up reminder" and m["Message-ID"] == f"<reminder-{d.id}@app.example.com>" and m["To"] == a.email
    text_wo_link = re.sub(r"https?://\S+", "", low)
    for phi in (TITLE.lower(), "hba1c", "patient", p.first_name.lower(), "diabet"): assert phi not in text_wo_link, phi
    assert f"https://app.example.com/hub?patient_id={p.id}#/followups" in body and "no health details" in low
    row = deliveries(db)[0]; assert row.status == "SENT" and row.attempts == 1 and row.sent_at is not None and row.lease_expires_at is None
    assert tick(S, when + timedelta(hours=2))["sent"] == 0 and len(smtp.messages) == 1           # at most once per (user, action, lead, due date)
    ev = db.scalar(select(AuditLog).where(AuditLog.action == "REMINDER_SENT")); assert ev.actor_user_id is None and ev.context == {"lead_days": 1, "attempts": 1}
    assert TITLE not in json.dumps([x.context for x in db.scalars(select(AuditLog)).all()], default=str)

def test_include_title_is_opt_in_and_sanitized(env, smtp):
    S, db, a, b, c, p, q = env
    mk_action(db, p, title="Visit\r\nBcc: attacker@example.com " + "x" * 300, due=due_in(2)); set_prefs(db, a, p, include_title=True)
    d = deliveries(db)[0]; tick(S, d.scheduled_for.replace(tzinfo=UTC) + timedelta(minutes=1))
    m, body = parsed(smtp.messages[0])
    assert "Follow-up: Visit Bcc: attacker@example.com" in body and m["Bcc"] is None and "\nBcc:" not in body and "x" * 200 not in body   # control chars stripped, length capped

# ---------------- retries / backoff / permanent failure ----------------
def test_transient_failure_backoff_then_terminal_failure_with_audit(env, smtp):
    S, db, a, b, c, p, q = env
    mk_action(db, p, due=due_in(2)); set_prefs(db, a, p); smtp.mode = "tempfail"
    t0 = deliveries(db)[0].scheduled_for.replace(tzinfo=UTC) + timedelta(minutes=1); t = t0
    for attempt in range(1, R.MAX_ATTEMPTS):
        tick(S, t); d = deliveries(db)[0]
        assert d.status == "PENDING" and d.attempts == attempt and d.last_error == "smtp_451"
        assert d.next_attempt_at.replace(tzinfo=UTC) == t + timedelta(seconds=R.backoff_seconds(attempt))
        assert tick(S, t + timedelta(seconds=R.backoff_seconds(attempt) - 1))["retry"] == 0 and deliveries(db)[0].attempts == attempt   # not retried early
        t = t + timedelta(seconds=R.backoff_seconds(attempt))
    tick(S, t); d = deliveries(db)[0]
    assert d.status == "FAILED" and d.attempts == R.MAX_ATTEMPTS and d.last_error == "smtp_451" and not smtp.messages
    fail = db.scalar(select(AuditLog).where(AuditLog.action == "REMINDER_FAILED")); assert fail.context["code"] == "smtp_451"
    smtp.mode = "ok"; assert tick(S, t + timedelta(hours=1))["sent"] == 0 and deliveries(db)[0].status == "FAILED"      # terminal: never silently resent

def test_transient_failure_then_recovery_sends_once(env, smtp):
    S, db, a, b, c, p, q = env
    mk_action(db, p, due=due_in(2)); set_prefs(db, a, p); smtp.mode = "tempfail"
    t = deliveries(db)[0].scheduled_for.replace(tzinfo=UTC) + timedelta(minutes=1); tick(S, t); assert deliveries(db)[0].status == "PENDING"
    smtp.mode = "ok"; tick(S, t + timedelta(seconds=61)); d = deliveries(db)[0]
    assert d.status == "SENT" and d.attempts == 2 and len(smtp.messages) == 1 and d.last_error is None

def test_permanent_failures_do_not_retry(env, smtp, monkeypatch):
    S, db, a, b, c, p, q = env
    mk_action(db, p, due=due_in(2)); set_prefs(db, a, p); smtp.mode = "reject"
    t = deliveries(db)[0].scheduled_for.replace(tzinfo=UTC) + timedelta(minutes=1); tick(S, t)
    d = deliveries(db)[0]; assert d.status == "FAILED" and d.attempts == 1 and d.last_error == "recipient_refused"
    with pytest.raises(EmailDeliveryError) as e: send_email(to="x@example.com", subject="s", body="b")
    assert e.value.permanent and e.value.code == "recipient_refused"

def test_smtp_not_configured_and_unreachable_are_classified(monkeypatch):
    for k in ("SMTP_HOST", "SMTP_FROM"): monkeypatch.delenv(k, raising=False)
    with pytest.raises(EmailDeliveryError) as e: send_email(to="x@example.com", subject="s", body="b")
    assert (e.value.code, e.value.permanent) == ("smtp_not_configured", True)
    monkeypatch.setenv("SMTP_HOST", "127.0.0.1"); monkeypatch.setenv("SMTP_PORT", "1"); monkeypatch.setenv("SMTP_FROM", "a@example.com"); monkeypatch.setenv("SMTP_STARTTLS", "false")
    with pytest.raises(EmailDeliveryError) as e: send_email(to="x@example.com", subject="s", body="b")
    assert (e.value.code, e.value.permanent) == ("smtp_unavailable", False)

def test_unexpected_sender_exception_is_contained_and_retried(env):
    S, db, a, b, c, p, q = env
    mk_action(db, p, due=due_in(2)); set_prefs(db, a, p)
    t = deliveries(db)[0].scheduled_for.replace(tzinfo=UTC) + timedelta(minutes=1)
    def boom(**kw): raise ValueError("secret detail " + TITLE)
    out = R.run_tick(S, sender=boom, now=t, force_reconcile=True); d = deliveries(db)[0]
    assert out["retry"] == 1 and d.status == "PENDING" and d.last_error == "unexpected_error" and "secret" not in str(d.last_error)

# ---------------- cancellation / expiration ----------------
def test_completing_cancelling_rescheduling_or_disabling_cancels_unsent(env, smtp):
    S, db, a, b, c, p, q = env
    act = mk_action(db, p, due=due_in(5)); set_prefs(db, a, p, lead_days=[1])
    d = deliveries(db)[0]; t = d.scheduled_for.replace(tzinfo=UTC) + timedelta(minutes=1)
    assert client(db, a).post(f"/api/health-actions/{act.id}/complete").status_code == 200
    out = tick(S, t); assert out["sent"] == 0 and not smtp.messages and deliveries(db)[0].status == "CANCELLED"
    act2 = mk_action(db, p, title="second", due=due_in(6)); R.reconcile(db); d2 = [x for x in deliveries(db) if x.action_id == act2.id][0]
    act2.due_date = due_in(9); db.commit(); R.reconcile(db)
    rows = [x for x in deliveries(db) if x.action_id == act2.id]
    assert {(x.due_date, x.status) for x in rows} == {(due_in(6), "CANCELLED"), (due_in(9), "PENDING")}
    assert set_prefs(db, a, p, enabled=False, lead_days=[]).status_code == 200
    assert all(x.status in ("CANCELLED",) for x in deliveries(db)) and tick(S, t + timedelta(days=30))["sent"] == 0
    assert set_prefs(db, a, p, lead_days=[1]).status_code == 200                                    # re-enabling revives the still-valid reminder
    assert [x.status for x in deliveries(db) if x.action_id == act2.id and x.due_date == due_in(9)] == ["PENDING"]

def test_action_completed_after_claim_before_send_is_not_sent(env, smtp):
    S, db, a, b, c, p, q = env
    act = mk_action(db, p, due=due_in(2)); set_prefs(db, a, p)
    t = deliveries(db)[0].scheduled_for.replace(tzinfo=UTC) + timedelta(minutes=1)
    ids = R.claim_due(db, t); assert len(ids) == 1
    act.status = HealthActionStatus.COMPLETED; db.commit()
    assert R.deliver(db, ids[0], now=t) == "CANCELLED" and not smtp.messages

def test_late_reminders_expire_instead_of_sending(env, smtp):
    S, db, a, b, c, p, q = env
    mk_action(db, p, due=due_in(3)); set_prefs(db, a, p)
    d = deliveries(db)[0]; late = d.scheduled_for.replace(tzinfo=UTC) + R.MAX_LATENESS + timedelta(minutes=1)
    out = tick(S, late); assert out["sent"] == 0 and deliveries(db)[0].status == "EXPIRED" and not smtp.messages

# ---------------- authorization re-checked at send time ----------------
@pytest.mark.parametrize("mutation", ["revoke", "expire", "drop_scope", "unverify", "suspend", "delete_patient"])
def test_access_changes_cancel_pending_reminders(env, smtp, mutation):
    S, db, a, b, c, p, q = env
    g = grant(db, a, p, b, ["HEALTH_ACTIONS"]); mk_action(db, p, due=due_in(2))
    assert set_prefs(db, b, p).status_code == 200
    d = deliveries(db, user_id=b.id)[0]; t = d.scheduled_for.replace(tzinfo=UTC) + timedelta(minutes=1)
    if mutation == "revoke": g.revoked_at = datetime.now(UTC)
    elif mutation == "expire": g.expires_at = t - timedelta(minutes=5)
    elif mutation == "drop_scope": g.scopes = ["PROFILE"]
    elif mutation == "unverify": b.email_verified = False
    elif mutation == "suspend": b.status = AccountStatus.DEACTIVATED
    elif mutation == "delete_patient": p.deletion_pending = True
    db.commit()
    out = tick(S, t); assert out["sent"] == 0 and not smtp.messages and deliveries(db, user_id=b.id)[0].status == "CANCELLED"

def test_owner_unaffected_by_caregiver_revocation(env, smtp):
    S, db, a, b, c, p, q = env
    g = grant(db, a, p, b, ["HEALTH_ACTIONS"]); mk_action(db, p, due=due_in(2)); set_prefs(db, a, p); set_prefs(db, b, p)
    g.revoked_at = datetime.now(UTC); db.commit()
    t = deliveries(db, user_id=a.id)[0].scheduled_for.replace(tzinfo=UTC) + timedelta(minutes=1); out = tick(S, t)
    assert out["sent"] == 1 and len(smtp.messages) == 1 and parsed(smtp.messages[0])[0]["To"] == a.email and b.email not in smtp.messages[0]

# ---------------- multi-worker safety / lease recovery ----------------
def test_claim_is_exclusive_and_lease_recovery(env):
    S, db, a, b, c, p, q = env
    for i in range(6): mk_action(db, p, title=f"a{i}", due=due_in(2))
    set_prefs(db, a, p); t = deliveries(db)[0].scheduled_for.replace(tzinfo=UTC) + timedelta(minutes=1)
    w1, w2 = S(), S()
    first = R.claim_due(w1, t, limit=4); second = R.claim_due(w2, t, limit=10)
    assert len(first) == 4 and len(second) == 2 and not set(first) & set(second)
    assert R.recover_leases(db, t + timedelta(seconds=R.LEASE_SECONDS - 5)) == 0
    assert R.recover_leases(db, t + timedelta(seconds=R.LEASE_SECONDS + 5)) == 6
    assert {d.status for d in deliveries(db)} == {"PENDING"}
    for d in deliveries(db)[:1]: d.status = "SENDING"; d.attempts = R.MAX_ATTEMPTS; d.lease_expires_at = t; db.commit()
    R.recover_leases(db, t + timedelta(hours=1)); assert deliveries(db)[0].status == "FAILED" and deliveries(db)[0].last_error == "lease_expired"

def test_parallel_workers_never_claim_the_same_delivery(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path}/c.db", connect_args={"timeout": 60}); Base.metadata.create_all(eng); S = sessionmaker(bind=eng)
    db = S(); u = User(username="u", email="u@example.com", password_hash="x", email_verified=True); db.add(u); db.flush()
    p = PatientProfile(owner_user_id=u.id, first_name="A", last_name="B"); db.add(p); db.flush()
    act = HealthAction(patient_id=p.id, title="t", action_type="FOLLOW_UP", source=HealthActionSource.USER_CREATED, provenance=Provenance.USER_REPORTED, status=HealthActionStatus.SCHEDULED, due_date=due_in(2)); db.add(act); db.flush()
    past = datetime.now(UTC) - timedelta(minutes=5)
    ids = []
    for i in range(40):
        d = ReminderDelivery(user_id=u.id, patient_id=p.id, action_id=act.id, lead_days=i % 6, due_date=due_in(2) + timedelta(days=i), scheduled_for=past, status="PENDING"); db.add(d); db.flush(); ids.append(d.id)
    db.commit(); db.close()
    results, barrier = [[], []], threading.Barrier(2)
    def worker(n):
        s = S(); barrier.wait(); results[n] = R.claim_due(s, datetime.now(UTC), limit=100); s.close()
    th = [threading.Thread(target=worker, args=(n,)) for n in (0, 1)]; [t.start() for t in th]; [t.join() for t in th]
    assert not set(results[0]) & set(results[1]) and set(results[0]) | set(results[1]) == set(ids)

# ---------------- visibility, IDOR, PHI ----------------
def test_delivery_listing_is_own_only_scoped_and_filterable(env, smtp):
    S, db, a, b, c, p, q = env
    grant(db, a, p, b, ["HEALTH_ACTIONS"]); mk_action(db, p, due=due_in(2)); set_prefs(db, a, p); set_prefs(db, b, p, lead_days=[0, 1])
    mine = client(db, a).get(f"/api/patients/{p.id}/reminder-deliveries").json(); theirs = client(db, b).get(f"/api/patients/{p.id}/reminder-deliveries").json()
    assert mine["total"] == 1 and theirs["total"] == 2 and not {x["id"] for x in mine["items"]} & {x["id"] for x in theirs["items"]}
    assert mine["items"][0]["action_title"] == TITLE and mine["items"][0]["status"] == "PENDING"
    assert client(db, a).get(f"/api/patients/{p.id}/reminder-deliveries?status=SENT").json()["total"] == 0
    assert client(db, a).get(f"/api/patients/{p.id}/reminder-deliveries?status=BOGUS").status_code == 422
    assert client(db, c).get(f"/api/patients/{p.id}/reminder-deliveries").status_code == 403
    g = db.scalar(select(PatientAccess).where(PatientAccess.user_id == b.id)); g.scopes = ["PROFILE"]; db.commit()
    assert client(db, b).get(f"/api/patients/{p.id}/reminder-deliveries").status_code == 403         # titles are health data: scope removed => gone

def test_admin_reminder_view_has_counts_only_and_raises_alerts(env, monkeypatch):
    S, db, a, b, c, p, q = env
    mk_action(db, p, due=due_in(2)); set_prefs(db, a, p)
    d = deliveries(db)[0]; d.status = "FAILED"; d.updated_at = datetime.now(UTC); db.commit()
    mk_action(db, p, title="Overdue-secret-title", due=due_in(3)); set_prefs(db, a, p)
    o = [x for x in deliveries(db) if x.status == "PENDING"][0]; o.scheduled_for = datetime.now(UTC) - timedelta(hours=3); db.commit()
    counts = ops.reminder_counts(db)
    assert counts["by_status"]["FAILED"] == 1 and counts["failed_24h"] == 1 and counts["overdue_pending"] == 1
    flat = json.dumps(counts)
    for needle in (TITLE, a.email, "Overdue-secret-title", p.first_name): assert needle not in flat
    monkeypatch.setattr(ops, "check_storage", lambda: {"status": "OK"})
    ops.evaluate_alerts(db)
    titles = {x.title for x in db.scalars(select(OpsAlert)).all()}
    assert "1 reminder email(s) failed in the last 24 hours" in titles and "1 reminder(s) are more than 1 hour overdue" in titles

# ---------------- worker integration ----------------
def test_worker_tick_dispatches_and_is_isolated(env, smtp, monkeypatch):
    S, db, a, b, c, p, q = env
    from jobs import worker
    mk_action(db, p, due=due_in(2)); set_prefs(db, a, p)
    t = deliveries(db)[0].scheduled_for.replace(tzinfo=UTC) + timedelta(minutes=1)
    monkeypatch.setattr(R, "now_utc", lambda: t)                                                         # the worker thread's clock: just after the reminder is due
    monkeypatch.setattr(worker, "SessionLocal", S); monkeypatch.setattr(R, "_last_reconcile", 0.0)
    worker.reminder_tick(); assert len(smtp.messages) == 1 and deliveries(db)[0].status == "SENT"
    monkeypatch.setattr(R, "run_tick", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    worker.reminder_tick()                                                                              # contained: document processing must continue
    monkeypatch.setenv("HEALTHGUARD_REMINDERS_ENABLED", "false")
    monkeypatch.setattr(R, "run_tick", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")))
    worker.reminder_tick()

def test_deleting_patient_removes_reminder_rows(env):
    S, db, a, b, c, p, q = env
    mk_action(db, p, due=due_in(2)); set_prefs(db, a, p); assert deliveries(db)
    assert client(db, a).delete(f"/api/patients/{p.id}").status_code == 204
    assert not deliveries(db) and db.scalar(select(ReminderPreference).where(ReminderPreference.patient_id == p.id)) is None

def test_migration_matches_models():
    text = (Path(__file__).resolve().parents[2] / "alembic/versions/0016_email_reminders.py").read_text()
    assert 'down_revision = "0015_schema_alignment"' in text and "def downgrade" in text
    for model in (ReminderPreference, ReminderDelivery):
        for col in model.__table__.columns: assert f'"{col.name}"' in text, (model.__tablename__, col.name)
    assert "uq_reminder_delivery_idempotency" in text and "uq_reminder_pref_user_patient" in text
