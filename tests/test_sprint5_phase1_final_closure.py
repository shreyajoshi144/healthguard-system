from __future__ import annotations

from datetime import datetime, timedelta, timezone
from io import BytesIO
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api.healthguard import router as healthguard_router
from backend.main import app
from backend import auth
from db.models import (
    AccessGrantStatus,
    AccessRole,
    AccessScope,
    AccountStatus,
    Base,
    Document,
    DocumentStatus,
    EventType,
    Handoff,
    HandoffStatus,
    HealthAction,
    HealthActionStatus,
    HealthEvent,
    Investigation,
    Measurement,
    InvestigationStatus,
    Medication,
    MedicationStatus,
    PatientAccess,
    PatientProfile,
    Provenance,
    User,
)
from db.session import get_db


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def _fk(conn, _record):
        conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        app.dependency_overrides.clear()


def client(db, user):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[auth.get_current_user] = lambda: user
    return TestClient(app)


def seed_users(db):
    owner = User(username=f"owner-{uuid4().hex[:8]}", email=f"owner-{uuid4().hex[:8]}@example.com", password_hash="x", email_verified=True)
    grantee = User(username=f"grant-{uuid4().hex[:8]}", email=f"grant-{uuid4().hex[:8]}@example.com", password_hash="x", email_verified=True)
    other = User(username=f"other-{uuid4().hex[:8]}", email=f"other-{uuid4().hex[:8]}@example.com", password_hash="x", email_verified=True)
    db.add_all([owner, grantee, other])
    db.flush()
    patient = PatientProfile(owner_user_id=owner.id, first_name="Owner", last_name="Patient")
    other_patient = PatientProfile(owner_user_id=other.id, first_name="Other", last_name="Patient")
    db.add_all([patient, other_patient])
    db.flush()
    return owner, grantee, other, patient, other_patient


def add_scope_grant(db, owner, patient, grantee, scopes, write_scopes=(), expires_at=None, role=AccessRole.FAMILY):
    grant = PatientAccess(
        patient_id=patient.id,
        user_id=grantee.id,
        role=role,
        granted_by=owner.id,
        status=AccessGrantStatus.ACTIVE,
        scopes=list(scopes),
        write_scopes=list(write_scopes),
        expires_at=expires_at,
    )
    db.add(grant)
    db.commit()
    return grant


def add_resources(db, owner, patient):
    document = Document(
        patient_id=patient.id,
        uploaded_by=owner.id,
        filename="record.pdf",
        content_type="application/pdf",
        file_size=16,
        storage_key=f"patients/{patient.id}/documents/{uuid4()}/record.pdf",
        checksum=uuid4().hex + "0" * 32,
        status=DocumentStatus.COMPLETED,
    )
    medication = Medication(patient_id=patient.id, medicine_name="Metformin", status=MedicationStatus.ACTIVE, provenance=Provenance.USER_REPORTED, created_by=owner.id)
    investigation = Investigation(patient_id=patient.id, name="CBC", status=InvestigationStatus.ORDERED, provenance=Provenance.USER_REPORTED, created_by=owner.id)
    event_row = HealthEvent(patient_id=patient.id, event_type=EventType.OTHER, title="Timeline event", provenance=Provenance.USER_REPORTED, created_by=owner.id)
    handoff = Handoff(patient_id=patient.id, title="Draft handoff", handoff_type="DOCTOR_HANDOFF", status=HandoffStatus.DRAFT, content="Draft", evidence_refs=[])
    action = HealthAction(patient_id=patient.id, title="Follow up", action_type="FOLLOW_UP", source="USER_CREATED", provenance=Provenance.USER_REPORTED, status=HealthActionStatus.PLANNED, temporal_precision="UNKNOWN")
    measurement = Measurement(patient_id=patient.id, kind="HEART_RATE", value=72.0, unit="bpm", measured_at=datetime.now(timezone.utc) - timedelta(minutes=5), provenance=Provenance.USER_REPORTED, created_by=owner.id)
    db.add_all([document, medication, investigation, event_row, handoff, action, measurement])
    db.flush()
    db.commit()
    return {
        "PROFILE": (f"/api/patients/{patient.id}", lambda c: c.patch(f"/api/patients/{patient.id}", json={"first_name": "Updated"})),
        "DOCUMENTS": (f"/api/patients/{patient.id}/documents", lambda c: c.post(f"/api/documents/{document.id}/retry-processing")),
        "MEDICATIONS": (f"/api/patients/{patient.id}/medications", lambda c: c.post(f"/api/patients/{patient.id}/medications", json={"medicine_name": "Aspirin"})),
        "INVESTIGATIONS": (f"/api/patients/{patient.id}/investigations", lambda c: c.post(f"/api/patients/{patient.id}/investigations", json={"name": "MRI"})),
        "TIMELINE": (f"/api/patients/{patient.id}/events", lambda c: c.post(f"/api/patients/{patient.id}/events", json={"event_type": "OTHER", "title": "New event"})),
        "HANDOFFS": (f"/api/patients/{patient.id}/handoffs", lambda c: c.patch(f"/api/handoffs/{handoff.id}", json={"title": "Updated draft"})),
        "HEALTH_ACTIONS": (f"/api/patients/{patient.id}/health-actions", lambda c: c.post(f"/api/patients/{patient.id}/health-actions", json={"title": "New action"})),
        "MEASUREMENTS": (f"/api/patients/{patient.id}/measurements", lambda c: c.post(f"/api/patients/{patient.id}/measurements", json={"kind": "HEART_RATE", "value": 70, "measured_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()})),
    }, document


SCOPE_READ_PATHS = {
    "PROFILE": lambda p: f"/api/patients/{p.id}",
    "DOCUMENTS": lambda p: f"/api/patients/{p.id}/documents",
    "MEDICATIONS": lambda p: f"/api/patients/{p.id}/medications",
    "INVESTIGATIONS": lambda p: f"/api/patients/{p.id}/investigations",
    "TIMELINE": lambda p: f"/api/patients/{p.id}/events",
    "HANDOFFS": lambda p: f"/api/patients/{p.id}/handoffs",
    "HEALTH_ACTIONS": lambda p: f"/api/patients/{p.id}/health-actions",
    "MEASUREMENTS": lambda p: f"/api/patients/{p.id}/measurements",
}

SCOPE_OTHER_PATHS = {
    "PROFILE": lambda p: f"/api/patients/{p.id}/medications",
    "DOCUMENTS": lambda p: f"/api/patients/{p.id}/events",
    "MEDICATIONS": lambda p: f"/api/patients/{p.id}/documents",
    "INVESTIGATIONS": lambda p: f"/api/patients/{p.id}/medications",
    "TIMELINE": lambda p: f"/api/patients/{p.id}/investigations",
    "HANDOFFS": lambda p: f"/api/patients/{p.id}/health-actions",
    "HEALTH_ACTIONS": lambda p: f"/api/patients/{p.id}/handoffs",
    "MEASUREMENTS": lambda p: f"/api/patients/{p.id}/events",
}


@pytest.mark.parametrize("scope", [s.value for s in AccessScope])
def test_every_access_scope_has_real_api_read_write_cross_patient_revocation_and_expiry(db, monkeypatch, scope):
    owner, grantee, other, patient, other_patient = seed_users(db)
    resources, document = add_resources(db, owner, patient)
    other_resources, other_document = add_resources(db, other, other_patient)
    if scope == "DOCUMENTS":
        document.status = DocumentStatus.FAILED
        other_document.status = DocumentStatus.FAILED
        db.commit()
        monkeypatch.setattr("api.healthguard.storage.get", lambda _key: b"ok")
        monkeypatch.setattr("api.healthguard.enqueue_document", lambda _job_id: None)

    add_scope_grant(db, owner, patient, grantee, [scope])
    c = client(db, grantee)

    # Authorized read succeeds.
    assert c.get(SCOPE_READ_PATHS[scope](patient)).status_code == 200

    # Unrelated read fails.
    assert c.get(SCOPE_OTHER_PATHS[scope](patient)).status_code == 403

    # Read-only access cannot write.
    assert resources[scope][1](c).status_code == 403

    # Explicit write_scope enables the real API mutation.
    grant = db.scalar(select(PatientAccess).where(PatientAccess.patient_id == patient.id, PatientAccess.user_id == grantee.id))
    grant.write_scopes = [scope]
    db.commit()
    result = resources[scope][1](c)
    assert result.status_code in {200, 201, 204}

    # Cross-patient patient-level access remains denied.
    assert c.get(SCOPE_READ_PATHS[scope](other_patient)).status_code == 403

    # Cross-patient resource IDs are denied where a resource endpoint exists.
    if scope == "DOCUMENTS":
        assert c.get(f"/api/documents/{other_document.id}").status_code == 403
    elif scope == "MEDICATIONS":
        other_med = db.scalar(select(Medication).where(Medication.patient_id == other_patient.id))
        assert c.get(f"/api/medications/{other_med.id}/events").status_code == 403
    elif scope == "TIMELINE":
        other_event = db.scalar(select(HealthEvent).where(HealthEvent.patient_id == other_patient.id))
        assert c.delete(f"/api/events/{other_event.id}").status_code == 403
    elif scope == "INVESTIGATIONS":
        other_inv = db.scalar(select(Investigation).where(Investigation.patient_id == other_patient.id))
        assert c.get(f"/api/investigations/{other_inv.id}/results").status_code == 403
    elif scope == "HANDOFFS":
        other_handoff = db.scalar(select(Handoff).where(Handoff.patient_id == other_patient.id))
        assert c.get(f"/api/handoffs/{other_handoff.id}").status_code == 403
    elif scope == "HEALTH_ACTIONS":
        other_action = db.scalar(select(HealthAction).where(HealthAction.patient_id == other_patient.id))
        assert c.get(f"/api/health-actions/{other_action.id}").status_code == 403
    elif scope == "MEASUREMENTS":
        other_m = db.scalar(select(Measurement).where(Measurement.patient_id == other_patient.id))
        assert c.delete(f"/api/measurements/{other_m.id}").status_code == 403
        assert c.patch(f"/api/measurements/{other_m.id}", json={"value": 80}).status_code == 403

    # Revocation is immediate.
    grant.revoked_at = datetime.now(timezone.utc)
    grant.status = AccessGrantStatus.REVOKED
    db.commit()
    assert c.get(SCOPE_READ_PATHS[scope](patient)).status_code == 403

    # Expiration is immediate.
    grant.revoked_at = None
    grant.status = AccessGrantStatus.ACTIVE
    grant.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db.commit()
    assert c.get(SCOPE_READ_PATHS[scope](patient)).status_code == 403


def test_ai_scope_authorization_happens_before_provider_for_ask_and_retrieve(db, monkeypatch):
    owner, grantee, _, patient, _ = seed_users(db)
    add_resources(db, owner, patient)
    add_scope_grant(db, owner, patient, grantee, ["MEDICATIONS"])
    calls = {"provider": 0}

    class Provider:
        def answer(self, **kwargs):
            calls["provider"] += 1
            raise AssertionError("provider must not be called when authorization fails")

    monkeypatch.setattr("ai.answer_provider.get_health_record_answer_provider", lambda: Provider())
    c = client(db, grantee)

    # retrieve is allowed only for a permitted retrieval scope and cannot expose other source families.
    retrieved = c.post(f"/api/patients/{patient.id}/retrieve", json={"query": "Metformin", "retrieval_mode": "deterministic"})
    assert retrieved.status_code == 200
    assert {x["source_type"] for x in retrieved.json()["evidence"]} <= {"MEDICATION", "MEDICATION_EVENT"}

    # Ask uses the same pre-provider authorization boundary.
    asked = c.post(f"/api/patients/{patient.id}/ask", json={"question": "Metformin", "retrieval_mode": "deterministic"})
    assert asked.status_code == 503 or asked.status_code == 200
    assert calls["provider"] <= 1

    # A scope with no retrieval sources is rejected before any provider call.
    grant = db.scalar(select(PatientAccess).where(PatientAccess.patient_id == patient.id, PatientAccess.user_id == grantee.id))
    grant.scopes = ["PROFILE"]
    db.commit()
    denied = c.post(f"/api/patients/{patient.id}/ask", json={"question": "anything"})
    assert denied.status_code == 403
    assert calls["provider"] <= 1


def test_health_snapshot_is_scope_filtered_at_api_boundary(db):
    owner, grantee, _, patient, _ = seed_users(db)
    add_resources(db, owner, patient)
    add_scope_grant(db, owner, patient, grantee, ["TIMELINE"])
    c = client(db, grantee)
    result = c.get(f"/api/patients/{patient.id}/health-snapshot")
    assert result.status_code == 200
    body = result.json()
    assert body["major_events"]
    assert body["medications"] == []
    assert body["investigations"] == []
    assert body["document_facts"] == []
    assert body["diagnoses_conditions"] == []
    assert body["vaccinations"] == []


def test_medication_context_cannot_invoke_provider_without_medication_scope(db, monkeypatch):
    owner, grantee, _, patient, _ = seed_users(db)
    resources, _ = add_resources(db, owner, patient)
    med = db.scalar(select(Medication).where(Medication.patient_id == patient.id))
    add_scope_grant(db, owner, patient, grantee, ["DOCUMENTS"])
    called = {"value": False}

    class Provider:
        def answer(self, **kwargs):
            called["value"] = True
            raise AssertionError("provider must not be called")

    monkeypatch.setattr("ai.medication_context_provider.get_medication_context_provider", lambda: Provider())
    r = client(db, grantee).post(f"/api/patients/{patient.id}/medications/{med.id}/context", json={"question": "What is this?"})
    assert r.status_code == 403
    assert called["value"] is False


def test_concurrent_style_retrieval_is_patient_isolated_without_shared_state(db):
    owner_a, user_a, owner_b, patient_a, patient_b = seed_users(db)
    # Reuse the two seeded patients as separate tenants; user_a gets patient A, owner_b owns patient B.
    add_scope_grant(db, owner_a, patient_a, user_a, ["MEDICATIONS"])
    med_a = Medication(patient_id=patient_a.id, medicine_name="A_ONLY", status=MedicationStatus.ACTIVE, provenance=Provenance.USER_REPORTED, created_by=owner_a.id)
    med_b = Medication(patient_id=patient_b.id, medicine_name="B_ONLY", status=MedicationStatus.ACTIVE, provenance=Provenance.USER_REPORTED, created_by=owner_b.id)
    db.add_all([med_a, med_b])
    db.commit()
    from domain.retrieval import retrieve_patient_evidence

    a = retrieve_patient_evidence(db, patient_a.id, "A_ONLY", allowed_scopes={"MEDICATIONS"})
    b = retrieve_patient_evidence(db, patient_b.id, "B_ONLY", allowed_scopes={"MEDICATIONS"})
    assert {x["value"] for x in a} == {"A_ONLY"}
    assert {x["value"] for x in b} == {"B_ONLY"}
    assert all(x["patient_id"] == patient_a.id for x in a)
    assert all(x["patient_id"] == patient_b.id for x in b)


def test_temporary_share_scopes_expiration_revocation_tampering_and_resource_ownership(db):
    from domain import rate_limit as rl
    rl._local.clear()
    owner, grantee, other, patient, other_patient = seed_users(db)
    resources, document = add_resources(db, owner, patient)
    other_resources, other_document = add_resources(db, other, other_patient)
    c_owner = client(db, owner)
    expiry = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()

    # Every supported resource scope is tested through the real share endpoint.
    scope_resource_ids = {
        "DOCUMENTS": [str(document.id)],
        "MEDICATIONS": [str(db.scalar(select(Medication).where(Medication.patient_id == patient.id)).id)],
        "INVESTIGATIONS": [str(db.scalar(select(Investigation).where(Investigation.patient_id == patient.id)).id)],
        "TIMELINE": [str(db.scalar(select(HealthEvent).where(HealthEvent.patient_id == patient.id)).id)],
        "HANDOFFS": [str(db.scalar(select(Handoff).where(Handoff.patient_id == patient.id)).id)],
        "HEALTH_ACTIONS": [str(db.scalar(select(HealthAction).where(HealthAction.patient_id == patient.id)).id)],
    }
    shared_handoff = db.scalar(select(Handoff).where(Handoff.patient_id == patient.id))
    shared_handoff.status = HandoffStatus.FINAL
    db.commit()
    for scope, ids in scope_resource_ids.items():
        r = c_owner.post(f"/api/patients/{patient.id}/shares", json={"scopes": [scope], "resources": {scope: ids}, "expires_at": expiry})
        assert r.status_code == 201
        token = r.json()["token"]
        assert c_owner.post("/api/shares/access", json={"token": token}).status_code == 200
        assert c_owner.post("/api/shares/access", json={"token": token + "tampered"}).status_code == 404

    # Cross-patient resource IDs cannot be embedded in a new share.
    bad = c_owner.post(f"/api/patients/{patient.id}/shares", json={"scopes": ["DOCUMENTS"], "resources": {"DOCUMENTS": [str(other_document.id)]}, "expires_at": expiry})
    assert bad.status_code in {404, 422}

    # Revocation and expiry invalidate the same bearer token.
    created = c_owner.post(f"/api/patients/{patient.id}/shares", json={"scopes": ["DOCUMENTS"], "resources": {"DOCUMENTS": [str(document.id)]}, "expires_at": expiry}).json()
    token = created["token"]
    assert c_owner.post(f"/api/shares/{created['id']}/revoke").status_code == 204
    assert c_owner.post("/api/shares/access", json={"token": token}).status_code == 404

    created = c_owner.post(f"/api/patients/{patient.id}/shares", json={"scopes": ["DOCUMENTS"], "resources": {"DOCUMENTS": [str(document.id)]}, "expires_at": expiry}).json()
    share = db.get(__import__("db.models", fromlist=["TemporaryShare"]).TemporaryShare, uuid4()) if False else None
    from db.models import TemporaryShare
    share_row = db.get(TemporaryShare, __import__("uuid").UUID(created["id"]))
    share_row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db.commit()
    assert c_owner.post("/api/shares/access", json={"token": created["token"]}).status_code == 404
    rl._local.clear()


def test_family_caregiver_server_side_escalation_is_denied(db):
    owner, family, caregiver, patient, _ = seed_users(db)
    add_scope_grant(db, owner, patient, family, ["MEDICATIONS"], role=AccessRole.FAMILY)
    add_scope_grant(db, owner, patient, caregiver, ["TIMELINE"], role=AccessRole.CAREGIVER)

    # The real grant endpoint rejects self-grants and OWNER role escalation.
    c_owner = client(db, owner)
    self_grant = c_owner.post(f"/api/patients/{patient.id}/access", json={"user_email": owner.email, "role": "FAMILY", "scopes": ["PROFILE"]})
    assert self_grant.status_code in {403, 409}
    owner_role = c_owner.post(f"/api/patients/{patient.id}/access", json={"user_email": family.email, "role": "OWNER", "scopes": ["PROFILE"]})
    assert owner_role.status_code == 422

    assert client(db, family).get(f"/api/patients/{patient.id}").status_code == 403
    assert client(db, caregiver).get(f"/api/patients/{patient.id}/events").status_code == 200



def test_document_upload_api_enforces_write_scope_and_storage_validation(db, monkeypatch):
    from domain import rate_limit as rl
    rl._local.clear()
    owner, grantee, _, patient, _ = seed_users(db)
    add_scope_grant(db, owner, patient, grantee, ["DOCUMENTS"], write_scopes=[])
    read_only = client(db, grantee)
    denied = read_only.post(
        f"/api/patients/{patient.id}/documents",
        files={"file": ("../../private.pdf", b"%PDF-1.7\n", "application/pdf")},
    )
    assert denied.status_code == 403

    grant = db.scalar(select(PatientAccess).where(PatientAccess.patient_id == patient.id, PatientAccess.user_id == grantee.id))
    grant.write_scopes = ["DOCUMENTS"]
    db.commit()
    monkeypatch.setattr("api.healthguard.storage.put_file", lambda *args, **kwargs: args[0])
    monkeypatch.setattr("api.healthguard.enqueue_document", lambda _job_id: None)
    allowed = client(db, grantee).post(
        f"/api/patients/{patient.id}/documents",
        files={"file": ("../../private.pdf", b"%PDF-1.7\n", "application/pdf")},
    )
    assert allowed.status_code == 201
    assert allowed.json()["filename"] == "private.pdf"

    mismatch = client(db, grantee).post(
        f"/api/patients/{patient.id}/documents",
        files={"file": ("record.pdf", b"not-a-pdf", "application/pdf")},
    )
    assert mismatch.status_code == 422
    assert "private_storage" not in mismatch.text
    rl._local.clear()

def test_provenance_cannot_be_client_upgraded_to_document_verified(db):
    owner, _, _, patient, _ = seed_users(db)
    c = client(db, owner)

    event = c.post(f"/api/patients/{patient.id}/events", json={"event_type": "OTHER", "title": "Manual", "provenance": "DOCUMENT_VERIFIED"})
    assert event.status_code == 201
    assert event.json()["provenance"] == "USER_REPORTED"

    medication = c.post(f"/api/patients/{patient.id}/medications", json={"medicine_name": "Client claimed", "provenance": "DOCUMENT_VERIFIED"})
    assert medication.status_code == 201
    assert medication.json()["provenance"] == "USER_REPORTED"

    investigation = c.post(f"/api/patients/{patient.id}/investigations", json={"name": "Client claimed", "provenance": "DOCUMENT_VERIFIED"})
    assert investigation.status_code == 201
    assert investigation.json()["provenance"] == "USER_REPORTED"

    action = c.post(f"/api/patients/{patient.id}/health-actions", json={"title": "Manual action", "source": "USER_CREATED", "provenance": "DOCUMENT_VERIFIED"})
    assert action.status_code == 201
    assert action.json()["provenance"] == "USER_REPORTED"


def test_production_configuration_rejects_unsafe_llm_and_redis(monkeypatch):
    import backend.main as main

    base = {
        "APP_ENV": "production",
        "DATABASE_URL": "postgresql+psycopg://u:p@db/healthguard",
        "REDIS_URL": "redis://redis:6379/0",
        "STORAGE_BACKEND": "s3",
        "STORAGE_ENDPOINT": "https://storage.internal",
        "STORAGE_BUCKET": "private",
        "STORAGE_ACCESS_KEY": "access",
        "STORAGE_SECRET_KEY": "secret",
        "SMTP_HOST": "smtp.internal",
        "SMTP_FROM": "healthguard@example.com",
        "HEALTHGUARD_LLM_PROVIDER": "openai_compatible",
        "HEALTHGUARD_LLM_BASE_URL": "https://llm.example.com/v1",
        "HEALTHGUARD_LLM_MODEL": "model",
        "HEALTHGUARD_LLM_API_KEY": "secret",
        "HEALTHGUARD_PUBLIC_BASE_URL": "https://healthguard.example.com",
        "HEALTHGUARD_AI_TIMEOUT_SECONDS": "45",
        "HEALTHGUARD_AI_MAX_RETRIES": "1",
        "HEALTHGUARD_EMBEDDING_PROVIDER": "openai_compatible",
        "HEALTHGUARD_EMBEDDING_BASE_URL": "https://embed.example.com/v1",
        "HEALTHGUARD_EMBEDDING_MODEL": "embed-model",
        "HEALTHGUARD_EMBEDDING_API_KEY": "secret",
        "DEBUG": "false",
    }
    for key, value in base.items():
        monkeypatch.setenv(key, value)
    main._validate_production_config()

    monkeypatch.setenv("HEALTHGUARD_LLM_BASE_URL", "http://llm.example.com/v1")
    with pytest.raises(RuntimeError, match="LLM_BASE_URL"):
        main._validate_production_config()

    monkeypatch.setenv("HEALTHGUARD_LLM_BASE_URL", "https://llm.example.com/v1")
    monkeypatch.setenv("REDIS_URL", "https://redis.example.com")
    with pytest.raises(RuntimeError, match="REDIS_URL"):
        main._validate_production_config()


def test_login_does_not_enumerate_unverified_accounts(db, monkeypatch):
    user = User(username="known", email="known@example.com", password_hash=auth._hash_password("correct horse battery staple"), email_verified=False, status=AccountStatus.ACTIVE)
    db.add(user)
    db.commit()
    monkeypatch.setattr("backend.main.send_verification_email", lambda *_: None)
    app.dependency_overrides[get_db] = lambda: db
    c = TestClient(app)
    unknown = c.post("/api/auth/login", json={"username": "unknown@example.com", "password": "wrong"})
    unverified = c.post("/api/auth/login", json={"username": "known@example.com", "password": "correct horse battery staple"})
    wrong_password = c.post("/api/auth/login", json={"username": "known@example.com", "password": "wrong"})
    assert unknown.status_code == unverified.status_code == wrong_password.status_code == 401
    assert unknown.json()["detail"] == unverified.json()["detail"] == wrong_password.json()["detail"]


def test_rate_limit_uses_authenticated_identity_and_fails_closed_in_production(monkeypatch):
    from domain import rate_limit as rl
    import asyncio

    class Client:
        host = "10.0.0.1"

    class Request:
        client = Client()
        headers = {"Authorization": "Bearer not-a-valid-token"}
        cookies = {}

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("REDIS_URL", raising=False)
    with pytest.raises(Exception) as exc:
        asyncio.run(rl.rate_limit("closure", 1)(Request()))
    assert getattr(exc.value, "status_code", None) == 503


def test_error_responses_do_not_expose_internal_storage_or_provider_details(db):
    owner, grantee, _, patient, _ = seed_users(db)
    add_scope_grant(db, owner, patient, grantee, ["DOCUMENTS"])
    response = client(db, grantee).get(f"/api/documents/{uuid4()}")
    assert response.status_code == 404
    body = response.text.lower()
    assert "traceback" not in body
    assert "/mnt/data" not in body
    assert "api_key" not in body
    assert "bearer" not in body


def test_finalizing_handoff_uses_reviewed_evidence_snapshot_not_changed_record(db, monkeypatch):
    from datetime import date
    from db.models import DatePrecision, Document, DocumentStatus, HealthFact, Provenance

    owner, grantee, _, patient, _ = seed_users(db)
    document = Document(
        patient_id=patient.id,
        uploaded_by=owner.id,
        filename="reviewed.pdf",
        content_type="application/pdf",
        file_size=10,
        storage_key=f"patients/{patient.id}/reviewed.pdf",
        checksum=uuid4().hex + "0" * 32,
        status=DocumentStatus.COMPLETED,
    )
    fact = HealthFact(
        patient_id=patient.id,
        document=document,
        fact_type="LAB_RESULT",
        value="Hemoglobin 13.2 g/dL",
        normalized_value="hemoglobin 13.2 g/dl",
        temporal_date=date(2025, 1, 1),
        temporal_precision=DatePrecision.EXACT,
        source_page=2,
        source_text="Hemoglobin 13.2 g/dL",
        confidence=0.9,
        extraction_method="TEST",
        provenance=Provenance.DOCUMENT_VERIFIED,
    )
    db.add_all([document, fact])
    db.commit()

    class Fake:
        def answer(self, *, question, evidence, system_instructions):
            item = next(x for x in evidence if "Hemoglobin 13.2" in str(x.get("value")))
            from ai.answer_provider import AnswerClaim, AnswerResult
            text = "Hemoglobin was 13.2 g/dL."
            return AnswerResult(text, [AnswerClaim(text, [item["evidence_id"]])], [item["evidence_id"]], True, None)

    monkeypatch.setattr("domain.handoff.get_health_record_answer_provider", lambda: Fake())
    c = client(db, owner)
    created = c.post(f"/api/patients/{patient.id}/handoffs", json={})
    assert created.status_code == 201
    handoff_id = created.json()["id"]
    assert created.json()["generation_metadata"]["evidence_snapshot"]

    # The underlying record changes after the draft was reviewed.
    fact.value = "Hemoglobin 99.9 g/dL"
    fact.normalized_value = "hemoglobin 99.9 g/dl"
    fact.source_text = "Hemoglobin 99.9 g/dL"
    db.commit()

    # A reviewer changing the draft to match the new record must not cause
    # finalization to silently ground against that new evidence.
    assert c.patch(
        f"/api/handoffs/{handoff_id}",
        json={"content": "The laboratory measurement was 99.9 g/dL."},
    ).status_code == 200
    finalized = c.post(f"/api/handoffs/{handoff_id}/finalize")
    assert finalized.status_code == 422
