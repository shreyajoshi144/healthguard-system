"""P0 admin console: default-deny RBAC, TOTP step-up, PHI isolation, job ops, alerts, AI ops, security ops, audit."""
from __future__ import annotations
import json, os, re, subprocess, sys, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
from sqlalchemy import select, text
from db.models import *
from domain import admin_ops as ops, security_events, observability
from domain import rate_limit as rl
from tests.test_sprint5_phase1_security import make_db, seed, client, grant, clean
from tests.p0.test_consumer import rich, SECRET

ROOT = Path(__file__).resolve().parents[2]
GET_ENDPOINTS = ["/api/admin/me", "/api/admin/overview", "/api/admin/deployment", "/api/admin/jobs", "/api/admin/ai", "/api/admin/security", "/api/admin/users", "/api/admin/alerts"]

@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("HEALTHGUARD_ADMIN_TOTP_KEY", "k" * 40)
    rl._local.clear()
    S = make_db(); db, a, b, c, p, q = seed(S)
    security_events.set_session_factory(S)
    yield S, db, a, b, c, p, q
    clean(); security_events.set_session_factory(__import__("db.session", fromlist=["SessionLocal"]).SessionLocal); rl._local.clear()

def make_admin(db, user, role=AdminRole.OPERATOR):
    g = AdminGrant(user_id=user.id, role=role, totp_salt="salt-" + user.username); db.add(g); db.commit(); return g

def login(db, user, grant_row, *, advance=0):
    c = client(db, user)
    code = ops.totp_now(grant_row.totp_salt, at=time.time() + advance)
    r = c.post("/api/admin/session", json={"code": code})
    return c, r

def admin_client(db, user, role=AdminRole.OPERATOR):
    g = make_admin(db, user, role); c, r = login(db, user, g); assert r.status_code == 200, r.text; return c, g

def add_job(db, owner_patient, owner, status, attempts=0, **kw):
    n = db.scalar(select(__import__("sqlalchemy").func.count()).select_from(Document)) or 0
    d = Document(patient_id=owner_patient.id, uploaded_by=owner.id, filename=f"PHI-FILENAME-{n}.pdf", content_type="application/pdf", file_size=1, storage_key=f"k{n}-{time.time_ns()}", checksum=f"{n:064d}", status=kw.pop("doc_status", DocumentStatus.FAILED))
    db.add(d); db.flush()
    j = ProcessingJob(document_id=d.id, status=status, attempts=attempts, **kw); db.add(j); db.commit(); return j, d

# ---------------- default deny / step-up ----------------
def test_console_invisible_until_totp_key_configured(monkeypatch):
    monkeypatch.delenv("HEALTHGUARD_ADMIN_TOTP_KEY", raising=False)
    S = make_db(); db, a, b, c, p, q = seed(S); make_admin(db, a)
    assert client(db, a).get("/api/admin/overview").status_code == 404
    clean()

@pytest.mark.parametrize("ep", GET_ENDPOINTS)
def test_regular_user_denied_every_endpoint_and_denial_is_recorded(env, ep):
    S, db, a, b, c, p, q = env
    assert client(db, c).get(ep).status_code == 403
    assert db.scalar(select(SecurityEvent).where(SecurityEvent.event_type == "ADMIN_ACCESS_DENIED")) is not None

def test_admin_without_totp_session_is_blocked_everywhere(env):
    S, db, a, b, c, p, q = env
    make_admin(db, a); u = client(db, a)
    for ep in GET_ENDPOINTS: assert u.get(ep).status_code == 428, ep
    assert u.post(f"/api/admin/alerts/evaluate").status_code == 428
    assert u.get("/api/admin/probe").json() == {"step_up_required": True}

def test_totp_wrong_code_replay_and_cookie_binding(env):
    S, db, a, b, c, p, q = env
    g = make_admin(db, a); gb = make_admin(db, b, AdminRole.VIEWER)
    u = client(db, a)
    assert u.post("/api/admin/session", json={"code": "000000"}).status_code == 403
    assert u.post("/api/admin/session", json={"code": "12ab56"}).status_code == 422
    code = ops.totp_now(g.totp_salt)
    assert u.post("/api/admin/session", json={"code": code}).status_code == 200
    assert u.get("/api/admin/me").json()["role"] == "OPERATOR"
    assert u.post("/api/admin/session", json={"code": code}).status_code == 403            # single-use: replay refused
    cookie = u.cookies.get("hg_admin"); assert cookie
    other = client(db, b); other.cookies.set("hg_admin", cookie, path="/api/admin")           # stolen cookie for another admin
    assert other.get("/api/admin/me").status_code == 428
    assert db.scalar(select(AuditLog).where(AuditLog.action == "ADMIN_SESSION_FAILED")) is not None
    assert db.scalar(select(SecurityEvent).where(SecurityEvent.event_type == "ADMIN_LOGIN_FAILED")) is not None

def test_totp_secret_is_not_stored_and_expired_cookie_rejected(env):
    S, db, a, b, c, p, q = env
    g = make_admin(db, a)
    secret_b32 = re.search(r"secret=([A-Z2-7]+)", ops.provisioning_uri("a", g.totp_salt)).group(1)
    assert not any(secret_b32 in str(v) for v in [g.totp_salt, g.last_totp_step])
    import jwt
    from backend.auth import SECRET_KEY, ALGORITHM
    old = jwt.encode({"sub": str(a.id), "typ": "admin", "gid": str(g.id), "sv": 1, "exp": datetime.now(timezone.utc) - timedelta(minutes=1)}, SECRET_KEY, algorithm=ALGORITHM)
    u = client(db, a); u.cookies.set("hg_admin", old, path="/api/admin")
    assert u.get("/api/admin/me").status_code == 428

def test_viewer_is_read_only_operator_can_act(env):
    S, db, a, b, c, p, q = env
    j, d = add_job(db, p, a, DocumentStatus.FAILED, attempts=1)
    v, _ = admin_client(db, a, AdminRole.VIEWER)
    for ep in GET_ENDPOINTS: assert v.get(ep).status_code == 200, ep
    assert v.post(f"/api/admin/jobs/{j.id}/retry").status_code == 403
    assert v.post(f"/api/admin/jobs/{j.id}/quarantine", json={"reason": "MANUAL_REVIEW"}).status_code == 403
    assert v.post("/api/admin/alerts/evaluate").status_code == 403
    assert v.post(f"/api/admin/users/{b.id}/revoke-sessions").status_code == 403

def test_admin_cannot_bypass_patient_authorization(env):
    S, db, a, b, c, p, q = env
    rich(db, a, p)
    adm, _ = admin_client(db, b)                          # b is an admin AND a normal user who does not own p
    for ep in ["dashboard", "summary", "doctor-questions", "health-graph", "unified-timeline", "medication-tracker", "documents", "medications", "events", "measurements"]:
        assert adm.get(f"/api/patients/{p.id}/{ep}").status_code == 403, ep

# ---------------- PHI isolation ----------------
def test_admin_surface_has_no_patient_routes_and_imports_no_patient_models():
    from backend.main import app
    admin_paths = [r.path for r in app.routes if getattr(r, "path", "").startswith("/api/admin")]
    assert admin_paths and not any("patient" in p.lower() or "document" in p.lower() for p in admin_paths)
    src = (ROOT / "api/admin.py").read_text() + (ROOT / "domain/admin_ops.py").read_text()
    for forbidden in ["HealthFact", "Medication", "Investigation", "HealthEvent", "Measurement", "Handoff", "DocumentTextBlock", "TemporaryShare", "PatientAccess"]:
        assert not re.search(rf"\b{forbidden}\b", src), forbidden
    assert not re.search(r"\.(filename|storage_key|first_name|last_name|date_of_birth|password_hash|email)\b(?!_masked)", src.replace("u.email", "MASKED_OK")), "admin code must not read PHI/credential columns"

def test_no_phi_in_any_admin_response(env):
    S, db, a, b, c, p, q = env
    rich(db, a, p)
    add_job(db, p, a, DocumentStatus.FAILED, attempts=3)
    adm, _ = admin_client(db, c)
    adm.post("/api/admin/alerts/evaluate")
    blob = " ".join(adm.get(ep).text for ep in GET_ENDPOINTS + ["/api/admin/jobs?state=POISONED", "/api/admin/security", "/api/admin/alerts"])
    for needle in ["PHI-FILENAME", "lab-2024.pdf", "Metformin", "Type 2 diabetes", "HbA1c", SECRET, "A Patient", "B Patient", "password_hash", "totp_salt", "storage_key", "rx.pdf", "x@example.com"]:
        assert needle not in blob, needle
    assert a.email not in blob and b.email not in blob                            # emails only ever masked

# ---------------- overview / deployment ----------------
def test_overview_components_queue_migration_and_unverified_list(env, monkeypatch):
    S, db, a, b, c, p, q = env
    add_job(db, p, a, DocumentStatus.QUEUED); add_job(db, p, a, DocumentStatus.COMPLETED)
    adm, _ = admin_client(db, a)
    o = adm.get("/api/admin/overview").json()
    assert o["components"]["database"]["status"] == "OK" and o["components"]["redis"]["status"] == "NOT_CONFIGURED"
    assert o["components"]["ai"]["live_probe"] == "NOT_PERFORMED" and o["jobs"]["by_status"]["QUEUED"] == 1 and o["jobs"]["by_status"]["COMPLETED"] == 1
    assert o["migration"]["status"] == "UNKNOWN" and any("real S3" in x for x in o["unverified"])
    from alembic.config import Config; from alembic.script import ScriptDirectory
    head = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini"))).get_current_head()
    db.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(255) NOT NULL)")); db.execute(text("INSERT INTO alembic_version VALUES (:h)"), {"h": head}); db.commit()
    assert adm.get("/api/admin/overview").json()["migration"] == {"status": "CURRENT", "current": head, "head": head}
    db.execute(text("UPDATE alembic_version SET version_num='0001_old'")); db.commit()
    assert adm.get("/api/admin/deployment").json()["migration"]["status"] == "BEHIND_OR_DIVERGED"

def test_deployment_reports_config_as_booleans_and_flags_unsafe_production(env, monkeypatch):
    S, db, a, b, c, p, q = env
    monkeypatch.setenv("HEALTHGUARD_JWT_SECRET", "S3CRET-VALUE-" + "x" * 30); monkeypatch.setenv("STORAGE_SECRET_KEY", "TOPSECRETKEY"); monkeypatch.setenv("HEALTHGUARD_LLM_API_KEY", "sk-LEAKME")
    adm, _ = admin_client(db, a)
    d = adm.get("/api/admin/deployment")
    for leak in ("S3CRET-VALUE", "TOPSECRETKEY", "sk-LEAKME", "k" * 40): assert leak not in d.text
    assert d.json()["configuration"]["jwt_secret_configured"] is True
    monkeypatch.setenv("APP_ENV", "production"); monkeypatch.delenv("REDIS_URL", raising=False)
    body = adm.get("/api/admin/deployment").json()
    assert "Redis not configured" in body["configuration_problems"] and body["healthy"] is False

# ---------------- jobs ----------------
def test_job_listing_states_filters_and_dto_fields(env):
    S, db, a, b, c, p, q = env
    now = datetime.now(timezone.utc)
    add_job(db, p, a, DocumentStatus.QUEUED, attempts=0); add_job(db, p, a, DocumentStatus.QUEUED, attempts=2)
    add_job(db, p, a, DocumentStatus.PROCESSING, attempts=1, lease_expires_at=now - timedelta(minutes=5), doc_status=DocumentStatus.PROCESSING)
    add_job(db, p, a, DocumentStatus.PROCESSING, attempts=1, lease_expires_at=now + timedelta(minutes=5), doc_status=DocumentStatus.PROCESSING)
    add_job(db, p, a, DocumentStatus.FAILED, attempts=3, last_error="lease_expired_max_attempts"); add_job(db, p, a, DocumentStatus.COMPLETED, attempts=1)
    add_job(db, p, a, DocumentStatus.FAILED, attempts=1, quarantined_at=now, quarantine_reason="MANUAL_REVIEW")
    adm, _ = admin_client(db, a)
    def states(q_): return sorted(i["state"] for i in adm.get(f"/api/admin/jobs{q_}").json()["items"])
    assert states("?state=STALE") == ["STALE"] and states("?state=RETRYING") == ["RETRYING"] and states("?state=POISONED") == ["POISONED"] and states("?state=QUARANTINED") == ["QUARANTINED"]
    assert states("?state=QUEUED") == ["QUEUED", "RETRYING"] and adm.get("/api/admin/jobs").json()["total"] == 7
    assert adm.get("/api/admin/jobs?state=BOGUS").status_code == 422
    c_ = adm.get("/api/admin/jobs").json()["counts"]
    assert c_["stale"] == 1 and c_["poisoned"] == 1 and c_["quarantined"] == 1 and c_["retrying"] == 1
    item = adm.get("/api/admin/jobs?state=POISONED").json()["items"][0]
    assert set(item) == {"id", "state", "status", "attempts", "max_attempts", "last_error_code", "created_at", "updated_at", "lease_expires_at", "next_attempt_at", "age_seconds", "processing_seconds", "heartbeat_age_seconds", "quarantined_at", "quarantine_reason", "document_ref"}
    assert len(adm.get("/api/admin/jobs?page_size=2").json()["items"]) == 2 and adm.get("/api/admin/jobs?page_size=101").status_code == 422

def test_retry_failed_stale_and_poisoned_jobs_with_audit_and_idempotency(env, monkeypatch):
    S, db, a, b, c, p, q = env
    sent = []; monkeypatch.setattr("jobs.queue.enqueue_document", lambda jid: sent.append(jid) or "queued")
    failed, fd = add_job(db, p, a, DocumentStatus.FAILED, attempts=1, last_error="x")
    poisoned, pd = add_job(db, p, a, DocumentStatus.FAILED, attempts=3)
    stale, sd = add_job(db, p, a, DocumentStatus.PROCESSING, attempts=1, lease_expires_at=datetime.now(timezone.utc) - timedelta(minutes=2), doc_status=DocumentStatus.PROCESSING)
    live, ld = add_job(db, p, a, DocumentStatus.PROCESSING, attempts=1, lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=2), doc_status=DocumentStatus.PROCESSING)
    adm, _ = admin_client(db, a)
    assert adm.post(f"/api/admin/jobs/{failed.id}/retry").json()["state"] == "QUEUED"
    db.refresh(failed); db.refresh(fd); assert failed.status == DocumentStatus.QUEUED and fd.status == DocumentStatus.QUEUED and failed.attempts == 1 and failed.last_error is None
    assert adm.post(f"/api/admin/jobs/{failed.id}/retry").json()["already_queued"] is True       # idempotent
    assert sent.count(str(failed.id)) == 1
    assert adm.post(f"/api/admin/jobs/{poisoned.id}/retry").status_code == 200; db.refresh(poisoned); assert poisoned.attempts == 0
    assert adm.post(f"/api/admin/jobs/{stale.id}/retry").status_code == 200
    assert adm.post(f"/api/admin/jobs/{live.id}/retry").status_code == 409                       # actively leased job is never stolen
    assert adm.post("/api/admin/jobs/00000000-0000-0000-0000-000000000000/retry").status_code == 404
    actions = [r.action for r in db.scalars(select(AuditLog)).all()]
    assert actions.count("ADMIN_JOB_RETRIED") == 3
    ctxs = [r.context for r in db.scalars(select(AuditLog).where(AuditLog.action == "ADMIN_JOB_RETRIED")).all()]
    assert all("filename" not in json.dumps(x) for x in ctxs) and {c_["previous_state"] for c_ in ctxs} == {"FAILED", "POISONED", "STALE"}

def test_retry_blocked_for_deleted_documents_and_when_queue_down(env, monkeypatch):
    S, db, a, b, c, p, q = env
    j, d = add_job(db, p, a, DocumentStatus.FAILED, doc_status=DocumentStatus.DELETED)
    adm, _ = admin_client(db, a)
    assert adm.post(f"/api/admin/jobs/{j.id}/retry").status_code == 409
    j2, d2 = add_job(db, p, a, DocumentStatus.FAILED)
    from jobs.queue import QueueUnavailable
    monkeypatch.setattr("jobs.queue.enqueue_document", lambda jid: (_ for _ in ()).throw(QueueUnavailable("down")))
    r = adm.post(f"/api/admin/jobs/{j2.id}/retry"); assert r.status_code == 503
    db.refresh(j2); assert j2.status == DocumentStatus.QUEUED                                    # durable QUEUED state survives for recovery

def test_quarantine_release_and_patient_retry_cannot_override(env, monkeypatch):
    S, db, a, b, c, p, q = env
    monkeypatch.setattr("jobs.queue.enqueue_document", lambda jid: "queued")
    j, d = add_job(db, p, a, DocumentStatus.QUEUED, attempts=2, doc_status=DocumentStatus.QUEUED)
    live, _ = add_job(db, p, a, DocumentStatus.PROCESSING, lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=3), doc_status=DocumentStatus.PROCESSING)
    adm, _ = admin_client(db, a)
    assert adm.post(f"/api/admin/jobs/{j.id}/quarantine", json={"reason": "not-allowed"}).status_code == 422
    assert adm.post(f"/api/admin/jobs/{j.id}/quarantine", json={"reason": "POISON_SUSPECTED", "extra": 1}).status_code == 422
    assert adm.post(f"/api/admin/jobs/{live.id}/quarantine", json={"reason": "MANUAL_REVIEW"}).status_code == 409
    assert adm.post(f"/api/admin/jobs/{j.id}/quarantine", json={"reason": "POISON_SUSPECTED"}).json()["state"] == "QUARANTINED"
    db.refresh(j); db.refresh(d); assert j.status == DocumentStatus.FAILED and d.status == DocumentStatus.FAILED and j.quarantine_reason == "POISON_SUSPECTED"
    assert adm.post(f"/api/admin/jobs/{j.id}/retry").status_code == 409
    owner = client(db, a)                                                                     # the patient's own retry is also held back
    assert owner.post(f"/api/documents/{d.id}/retry-processing").status_code == 409
    assert adm.post(f"/api/admin/jobs/{j.id}/release").json()["state"] == "FAILED"
    assert adm.post(f"/api/admin/jobs/{j.id}/release").status_code == 409
    acts = {r.action for r in db.scalars(select(AuditLog)).all()}
    assert {"ADMIN_JOB_QUARANTINED", "ADMIN_JOB_RELEASED"} <= acts

def test_claimed_job_state_machine_still_refuses_quarantined(env):
    S, db, a, b, c, p, q = env
    j, d = add_job(db, p, a, DocumentStatus.FAILED, quarantined_at=datetime.now(timezone.utc), quarantine_reason="MANUAL_REVIEW")
    from jobs import worker
    monkey_session = lambda: S()
    worker.SessionLocal = monkey_session
    assert worker._claim_job(db, j.id) is False

# ---------------- alerts ----------------
def test_alert_lifecycle_dedupe_autoresolve_and_honest_delivery(env, monkeypatch):
    S, db, a, b, c, p, q = env
    monkeypatch.setattr(ops, "check_storage", lambda: {"status": "OK", "backend": "local"})        # hermetic: storage is healthy here
    add_job(db, p, a, DocumentStatus.FAILED, attempts=3)
    adm, _ = admin_client(db, a)
    r = adm.post("/api/admin/alerts/evaluate").json(); assert r["firing"] == 2
    adm.post("/api/admin/alerts/evaluate")
    al = adm.get("/api/admin/alerts?status=OPEN").json()
    assert al["total"] >= 2 and all(x["delivery"]["state"] == "NOT_CONFIGURED" and x["delivery"]["attempts"] == 0 for x in al["items"])
    assert "no notification is sent" in al["delivery_note"]
    failed_alert = next(x for x in al["items"] if x["title"] == "1 document-processing job(s) failed"); assert failed_alert["occurrences"] == 2 and failed_alert["severity"] == "WARNING"
    assert adm.post(f"/api/admin/alerts/{failed_alert['id']}/acknowledge").json()["status"] == "ACKNOWLEDGED"
    assert adm.post(f"/api/admin/alerts/{failed_alert['id']}/acknowledge").status_code == 409
    assert adm.post(f"/api/admin/alerts/{failed_alert['id']}/resolve").json()["status"] == "RESOLVED"
    assert adm.post(f"/api/admin/alerts/{failed_alert['id']}/resolve").status_code == 409
    db.execute(text("DELETE FROM processing_jobs")); db.commit()
    res = adm.post("/api/admin/alerts/evaluate").json(); assert res["firing"] == 0 and res["auto_resolved"] >= 1
    assert adm.get("/api/admin/alerts?status=OPEN").json()["total"] == 0
    assert adm.post("/api/admin/alerts/00000000-0000-0000-0000-000000000000/resolve").status_code == 404

def test_alert_for_auth_failure_spike_and_critical_failed_jobs(env, monkeypatch):
    S, db, a, b, c, p, q = env
    monkeypatch.setattr(ops, "AUTH_FAIL_SPIKE", 3)
    for _ in range(4): db.add(SecurityEvent(event_type="AUTH_FAILURE", route="/api/x", method="GET", status_code=401))
    for _ in range(5): add_job(db, p, a, DocumentStatus.FAILED, attempts=1)
    db.commit(); adm, _ = admin_client(db, a); adm.post("/api/admin/alerts/evaluate")
    titles = {x["title"]: x["severity"] for x in adm.get("/api/admin/alerts").json()["items"]}
    assert any("authentication failures" in t for t in titles) and titles["5 document-processing job(s) failed"] == "CRITICAL"

# ---------------- AI ops ----------------
def test_ai_ops_aggregates_real_counters_and_never_invents_cost(env):
    S, db, a, b, c, p, q = env
    before = adm_ai = None
    adm, _ = admin_client(db, a)
    base = adm.get("/api/admin/ai").json()
    observability.inc("healthguard_ai_provider_calls_total", 3, outcome="success", model="m-test")
    observability.inc("healthguard_ai_provider_calls_total", 1, outcome="failure", model="m-test")
    observability.observe("healthguard_ai_provider_latency", 200.0, model="m-test"); observability.observe("healthguard_ai_provider_latency", 400.0, model="m-test")
    observability.inc("healthguard_ai_provider_attempts_total", 2, outcome="failure", model="m-test", error_type="TimeoutError")
    observability.inc("healthguard_ai_safety_rejections_total", 2, surface="ask"); observability.inc("healthguard_ai_grounding_rejections_total", 1, surface="ask", reason="uncited claim")
    r = adm.get("/api/admin/ai").json()
    assert r["requests"]["succeeded"] - base["requests"]["succeeded"] == 3 and r["requests"]["failed"] - base["requests"]["failed"] == 1
    assert r["availability_ratio"] is not None and r["latency_ms"]["average"] is not None
    assert r["failed_attempts_by_type"]["TimeoutError"] >= 2 and r["timeouts_or_retries"] >= 2
    assert r["safety_rejections_by_surface"]["ask"] >= 2 and r["grounding_rejections"]["ask:uncited claim"] >= 1
    # These calls reported no usage: they must add no tokens, and with no prices configured no cost may be reported (never invented).
    assert r["tokens_and_cost"]["tokens"] == base["tokens_and_cost"]["tokens"] and r["tokens_and_cost"]["cost"]["available"] is False and "scope" in r

# ---------------- security ops ----------------
def test_security_feed_records_auth_and_authz_failures_without_query_or_identity_leak(env):
    S, db, a, b, c, p, q = env
    anon = client(db, a); clean()                         # no dependency override -> real auth
    from fastapi.testclient import TestClient
    from backend.main import app
    app.dependency_overrides[__import__("db.session", fromlist=["get_db"]).get_db] = lambda: db
    t = TestClient(app)
    assert t.get(f"/api/patients/{p.id}/summary?token=SUPERSECRET").status_code == 401
    u = client(db, c); assert u.get(f"/api/patients/{p.id}/summary?token=SUPERSECRET").status_code == 403
    rows = db.scalars(select(SecurityEvent)).all()
    types = {r.event_type for r in rows}; assert {"AUTH_FAILURE", "AUTHORIZATION_FAILURE"} <= types
    assert all("SUPERSECRET" not in (r.route + (r.request_id or "")) and str(p.id) not in r.route and r.route == "/api/patients/{patient_id}/summary" for r in rows)
    adm, _ = admin_client(db, b)
    s = adm.get("/api/admin/security").json()
    assert s["counts_by_type"]["AUTH_FAILURE"] >= 1 and s["counts_by_type"]["AUTHORIZATION_FAILURE"] >= 1
    assert "SUPERSECRET" not in json.dumps(s) and str(c.id) not in json.dumps(s) and "context" not in json.dumps(s["recent_admin_audit"])

def test_security_feed_includes_sharing_audit_and_admin_actions(env):
    S, db, a, b, c, p, q = env
    db.add(AuditLog(actor_user_id=a.id, action="SHARE_CREATED", resource_type="temporary_share", resource_id="x", context={"scopes": ["MEDICATIONS"]})); db.commit()
    adm, _ = admin_client(db, b)
    s = adm.get("/api/admin/security").json()
    assert s["audit_counts"]["SHARE_CREATED"] == 1 and any(x["action"] == "SHARE_CREATED" for x in s["recent_sharing_audit"])
    assert any(x["action"] == "ADMIN_SESSION_STARTED" for x in s["recent_admin_audit"])

# ---------------- users ----------------
def test_user_directory_is_operational_metadata_only_and_revoke_sessions(env):
    S, db, a, b, c, p, q = env
    adm, _ = admin_client(db, c)
    r = adm.get("/api/admin/users").json()
    assert r["total"] == 3 and all(set(u) == {"id", "username", "email_masked", "status", "email_verified", "deletion_pending", "session_version", "created_at", "updated_at", "patient_profile_count", "is_admin"} for u in r["items"])
    mine = next(u for u in r["items"] if u["id"] == str(a.id)); assert mine["patient_profile_count"] == 1 and mine["email_masked"].startswith(a.email[0] + "***@") and a.email not in json.dumps(r)
    assert next(u for u in r["items"] if u["id"] == str(c.id))["is_admin"] is True
    assert adm.get("/api/admin/users?q=a-").json()["total"] == 1 and adm.get("/api/admin/users?q=%27%3B--").status_code == 422
    v0 = a.session_version
    assert adm.post(f"/api/admin/users/{a.id}/revoke-sessions").json()["session_version"] == v0 + 1
    assert adm.post(f"/api/admin/users/{c.id}/revoke-sessions").status_code == 409            # not on yourself
    assert adm.post("/api/admin/users/00000000-0000-0000-0000-000000000000/revoke-sessions").status_code == 404
    acts = [r_.action for r_ in db.scalars(select(AuditLog)).all()]
    assert "ADMIN_USERS_VIEWED" in acts and "ADMIN_USER_SESSIONS_REVOKED" in acts

def test_revoked_admin_grant_loses_access_immediately(env):
    S, db, a, b, c, p, q = env
    adm, g = admin_client(db, a)
    assert adm.get("/api/admin/overview").status_code == 200
    g.revoked_at = datetime.now(timezone.utc); db.commit()
    assert adm.get("/api/admin/overview").status_code == 403

# ---------------- provisioning CLI ----------------
def test_cli_grant_prints_working_totp_uri_and_revoke(tmp_path):
    dburl = f"sqlite:///{tmp_path}/t.db"
    env = {**os.environ, "DATABASE_URL": dburl, "HEALTHGUARD_ADMIN_TOTP_KEY": "q" * 40, "PYTHONPATH": str(ROOT)}
    subprocess.run([sys.executable, "-c", "from db.session import engine; from db.models import Base, User; Base.metadata.create_all(engine); from db.session import SessionLocal; s=SessionLocal(); s.add(User(username='opsuser',email='ops@example.com',password_hash='x',email_verified=True)); s.commit()"], env=env, cwd=ROOT, check=True)
    out = subprocess.run([sys.executable, "scripts/admin.py", "grant", "opsuser", "--role", "OPERATOR"], env=env, cwd=ROOT, capture_output=True, text=True)
    assert out.returncode == 0 and "otpauth://totp/" in out.stdout
    again = subprocess.run([sys.executable, "scripts/admin.py", "grant", "opsuser"], env=env, cwd=ROOT, capture_output=True, text=True); assert again.returncode == 1
    lst = subprocess.run([sys.executable, "scripts/admin.py", "list"], env=env, cwd=ROOT, capture_output=True, text=True); assert "opsuser\tOPERATOR\tACTIVE" in lst.stdout
    assert subprocess.run([sys.executable, "scripts/admin.py", "revoke", "opsuser"], env=env, cwd=ROOT).returncode == 0
    nokey = subprocess.run([sys.executable, "scripts/admin.py", "grant", "opsuser"], env={k: v for k, v in env.items() if k != "HEALTHGUARD_ADMIN_TOTP_KEY"}, cwd=ROOT, capture_output=True, text=True); assert nokey.returncode == 2

def test_totp_rfc6238_vector():
    # RFC 6238 Appendix B SHA1 vectors (secret "12345678901234567890"), last 6 digits of the 8-digit values
    secret = b"12345678901234567890"
    assert ops._hotp(secret, 59 // 30) == "287082" and ops._hotp(secret, 1111111109 // 30) == "081804"

def test_storage_down_raises_critical_alert(env, monkeypatch):
    S, db, a, b, c, p, q = env
    monkeypatch.setattr(ops, "check_storage", lambda: {"status": "DOWN", "error_type": "ClientError"})
    adm, _ = admin_client(db, a); adm.post("/api/admin/alerts/evaluate")
    items = adm.get("/api/admin/alerts?status=OPEN").json()["items"]
    assert [x["severity"] for x in items if x["source"] == "storage"] == ["CRITICAL"]
