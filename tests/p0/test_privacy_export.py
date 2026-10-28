"""Consent and patient-data-export regression tests."""
from __future__ import annotations

import io
import json
import zipfile
from datetime import date

import pytest

from db.models import *
from domain.consent import PURPOSES, external_ai_configured, has_consent, owner_consented
from domain.export import EXPORTED_TABLES, NOT_EXPORTED, build_patient_export
from tests.test_sprint5_phase1_security import clean, client, grant, make_db, seed


@pytest.fixture
def world():
    S = make_db(); db, a, b, c, p, q = seed(S)
    live = Document(patient_id=p.id, uploaded_by=a.id, filename="lab.pdf", content_type="application/pdf", file_size=3, storage_key="k-live", checksum="1" * 64,
                    status=DocumentStatus.COMPLETED, document_type=DocumentType.LAB_REPORT)
    gone = Document(patient_id=p.id, uploaded_by=a.id, filename="gone.pdf", content_type="application/pdf", file_size=3, storage_key="k-gone", checksum="2" * 64,
                    status=DocumentStatus.DELETED, document_type=DocumentType.LAB_REPORT)
    db.add_all([live, gone]); db.flush()
    db.add_all([DocumentTextBlock(document_id=live.id, page_number=1, block_index=0, source_text="LIVETEXT hemoglobin", extraction_method="pdf_text"),
                DocumentTextBlock(document_id=gone.id, page_number=1, block_index=0, source_text="GONETEXT secret", extraction_method="pdf_text"),
                HealthFact(patient_id=p.id, document_id=live.id, fact_type="LAB_RESULT", value="Hemoglobin 14.2 g/dL", source_page=1, source_text="Hemoglobin 14.2 g/dL", extraction_method="stub"),
                HealthEvent(patient_id=p.id, event_type=EventType.CONSULTATION, title="Cardiology visit", event_date=date(2024, 5, 10), created_by=a.id),
                Medication(patient_id=p.id, medicine_name="Metformin", dose="500 mg", status=MedicationStatus.ACTIVE, created_by=a.id),
                HealthEvent(patient_id=q.id, event_type=EventType.OTHER, title="OTHERPATIENT event", created_by=b.id)])
    db.commit()
    yield db, a, b, c, p, q, live, gone
    clean()


# ----------------------------------------------------------------------------------------------- consent
def test_consent_defaults_to_not_granted_and_can_be_granted_and_withdrawn_with_audit(world):
    db, a, b, c, p, q, *_ = world
    cl = client(db, a)
    first = cl.get("/api/privacy/consents").json()
    assert first[0]["purpose"] == "AI_PROCESSING" and first[0]["granted"] is False and "no-store" in cl.get("/api/privacy/consents").headers["cache-control"]
    assert cl.put("/api/privacy/consents/AI_PROCESSING", json={"granted": True}).json()["granted"] is True
    assert has_consent(db, a.id, "AI_PROCESSING")
    assert cl.put("/api/privacy/consents/AI_PROCESSING", json={"granted": False}).json()["granted"] is False
    assert not has_consent(db, a.id, "AI_PROCESSING")
    actions = [r.action for r in db.query(AuditLog).filter(AuditLog.action.like("CONSENT_%")).order_by(AuditLog.occurred_at)]
    assert actions == ["CONSENT_GRANTED", "CONSENT_WITHDRAWN"]
    assert all("question" not in (r.context or {}) for r in db.query(AuditLog))
    assert cl.put("/api/privacy/consents/NOPE", json={"granted": True}).status_code == 404


def test_consent_is_per_user_and_policy_version_change_invalidates_it(world, monkeypatch):
    db, a, b, *_ = world
    client(db, a).put("/api/privacy/consents/AI_PROCESSING", json={"granted": True})
    assert has_consent(db, a.id, "AI_PROCESSING") and not has_consent(db, b.id, "AI_PROCESSING")
    monkeypatch.setitem(PURPOSES["AI_PROCESSING"], "version", "2")           # wording changed -> must re-consent
    assert not has_consent(db, a.id, "AI_PROCESSING")
    assert client(db, a).get("/api/privacy/consents").json()[0]["needs_reconsent"] is True


def test_no_consent_needed_when_no_external_ai_is_configured(world, monkeypatch):
    db, a, b, c, p, *_ = world
    monkeypatch.delenv("HEALTHGUARD_LLM_API_KEY", raising=False)
    assert not external_ai_configured() and owner_consented(db, p)


def test_ask_and_handoff_are_blocked_without_owner_consent_when_external_ai_is_configured(world, monkeypatch):
    db, a, b, c, p, *_ = world
    monkeypatch.setenv("HEALTHGUARD_LLM_API_KEY", "test-key")
    r = client(db, a).post(f"/api/patients/{p.id}/ask", json={"question": "What was my hemoglobin?"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "CONSENT_REQUIRED"
    assert client(db, a).post(f"/api/patients/{p.id}/handoffs", json={"title": "t", "handoff_type": "SECOND_OPINION"}).status_code in {403, 422}
    r2 = client(db, a).post(f"/api/patients/{p.id}/handoffs", json={"title": "Visit", "handoff_type": "DOCTOR_VISIT"})
    assert r2.status_code == 403 and r2.json()["detail"]["code"] == "CONSENT_REQUIRED"


def test_a_grantee_cannot_bypass_the_owners_missing_consent(world, monkeypatch):
    db, a, b, c, p, *_ = world
    grant(db, a, p, b, ["PROFILE", "DOCUMENTS", "MEDICATIONS", "TIMELINE", "HANDOFFS", "HEALTH_ACTIONS", "HEALTH_FACTS", "INVESTIGATIONS"])
    client(db, b).put("/api/privacy/consents/AI_PROCESSING", json={"granted": True})     # the requester's own consent is not the record owner's
    monkeypatch.setenv("HEALTHGUARD_LLM_API_KEY", "test-key")
    r = client(db, b).post(f"/api/patients/{p.id}/ask", json={"question": "What was my hemoglobin?"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "CONSENT_REQUIRED"


def test_with_owner_consent_the_ask_gate_is_passed(world, monkeypatch):
    db, a, b, c, p, *_ = world
    monkeypatch.setenv("HEALTHGUARD_LLM_API_KEY", "test-key")
    client(db, a).put("/api/privacy/consents/AI_PROCESSING", json={"granted": True})
    r = client(db, a).post(f"/api/patients/{p.id}/ask", json={"question": "What was my hemoglobin?"})
    assert r.status_code != 403      # reaches retrieval / provider (which is unconfigured here, so a different status is fine)


def test_worker_fails_without_retry_when_consent_is_missing_and_granting_consent_requeues(world, monkeypatch):
    db, a, b, c, p, q, live, gone = world
    import jobs.worker as w
    monkeypatch.setenv("HEALTHGUARD_LLM_API_KEY", "test-key")
    job = ProcessingJob(document_id=live.id, status=DocumentStatus.PROCESSING, attempts=1); live.status = DocumentStatus.PROCESSING
    db.add(job); db.commit(); job_id, live_id = job.id, live.id
    from sqlalchemy.orm import sessionmaker
    monkeypatch.setattr(w, "SessionLocal", sessionmaker(bind=db.get_bind()))      # the worker uses its own session, as in production
    called = []
    monkeypatch.setattr(w, "get_production_provider", lambda: called.append(1))
    w._mark_consent_blocked(job_id, live_id)
    db.expire_all()
    assert db.get(ProcessingJob, job_id).status == DocumentStatus.FAILED and db.get(ProcessingJob, job_id).last_error == w.CONSENT_BLOCKED and not called
    monkeypatch.setattr("jobs.queue.enqueue_document", lambda jid: "ok")
    out = client(db, a).put("/api/privacy/consents/AI_PROCESSING", json={"granted": True}).json()
    db.expire_all()
    assert out["requeued_documents"] == 1 and db.get(ProcessingJob, job_id).status == DocumentStatus.QUEUED and db.get(Document, live_id).status == DocumentStatus.QUEUED


# ----------------------------------------------------------------------------------------------- export
def test_every_table_has_an_explicit_export_decision():
    from db.models import Base
    decided = set(EXPORTED_TABLES) | set(NOT_EXPORTED)
    assert set(Base.metadata.tables) <= decided, f"decide export for new table(s): {set(Base.metadata.tables) - decided}"
    assert not set(EXPORTED_TABLES) & set(NOT_EXPORTED) and all(NOT_EXPORTED.values())


def test_export_contains_the_patients_records_and_nothing_else(world):
    db, a, b, c, p, q, live, gone = world
    r = client(db, a).get(f"/api/patients/{p.id}/export")
    assert r.status_code == 200 and r.headers["content-disposition"].startswith("attachment") and "no-store" in r.headers["cache-control"]
    body = r.text; data = r.json()
    assert data["schema_version"] == "healthguard.export.v1" and data["patient_id"] == str(p.id)
    assert "LIVETEXT" in body and "Hemoglobin 14.2" in body and "Cardiology visit" in body and "Metformin" in body
    assert "GONETEXT" not in body and "gone.pdf" not in body, "deleted documents must not be exported"
    assert "OTHERPATIENT" not in body, "another patient's data must never appear"
    for secret in ("password_hash", "token_hash", "storage_key", "k-live", "k-gone"):
        assert secret not in body
    assert data["counts"]["documents"] == 1 and data["counts"]["health_events"] == 1


def test_export_keeps_user_entries_but_drops_citations_to_deleted_documents(world):
    db, a, b, c, p, q, live, gone = world
    db.add(Measurement(patient_id=p.id, kind="WEIGHT", value=72, unit="kg", measured_at=__import__("datetime").datetime(2024, 1, 1, tzinfo=__import__("datetime").timezone.utc),
                       provenance=Provenance.USER_REPORTED, source_type=EventSource.DOCUMENT, source_document_id=gone.id, created_by=a.id)); db.commit()
    data = client(db, a).get(f"/api/patients/{p.id}/export").json()
    assert data["measurements"] and data["measurements"][0]["source_document_id"] is None


def test_only_the_owner_can_export_not_grantees_or_strangers(world):
    db, a, b, c, p, *_ = world
    grant(db, a, p, b, ["PROFILE", "DOCUMENTS", "MEDICATIONS", "TIMELINE"])
    assert client(db, b).get(f"/api/patients/{p.id}/export").status_code == 403
    assert client(db, c).get(f"/api/patients/{p.id}/export").status_code == 403
    assert client(db, a).get("/api/patients/00000000-0000-0000-0000-000000000000/export").status_code == 404


def test_export_masks_grantee_email_and_records_an_audit_event_with_counts_only(world):
    db, a, b, c, p, *_ = world
    grant(db, a, p, b, ["PROFILE"])
    r = client(db, a).get(f"/api/patients/{p.id}/export")
    assert b.email not in r.text and "***@" in r.text
    ev = db.query(AuditLog).filter_by(action="PATIENT_EXPORTED").one()
    assert ev.context["counts"]["documents"] == 1 and "Hemoglobin" not in json.dumps(ev.context)


def test_zip_export_contains_original_files_and_reports_unreadable_ones(world, monkeypatch):
    db, a, b, c, p, q, live, gone = world
    import api.privacy as pv
    monkeypatch.setattr(pv.storage, "get", lambda key: b"%PDF-original-bytes")
    z = zipfile.ZipFile(io.BytesIO(client(db, a).get(f"/api/patients/{p.id}/export?format=zip").content))
    names = z.namelist()
    assert "healthguard-export.json" in names and "README.txt" in names and any(n.startswith("documents/") and n.endswith("lab.pdf") for n in names)
    assert not any("gone" in n for n in names) and z.read([n for n in names if n.startswith("documents/")][0]) == b"%PDF-original-bytes"
    monkeypatch.setattr(pv.storage, "get", lambda key: (_ for _ in ()).throw(RuntimeError("s3 down")))
    z2 = zipfile.ZipFile(io.BytesIO(client(db, a).get(f"/api/patients/{p.id}/export?format=zip").content))
    assert json.loads(z2.read("healthguard-export.json"))["original_files_not_included"] == [str(live.id)]


def test_export_is_rate_limited(world):
    db, a, b, c, p, *_ = world
    cl = client(db, a)
    codes = [cl.get(f"/api/patients/{p.id}/export").status_code for _ in range(7)]
    assert codes[:5] == [200] * 5 and 429 in codes[5:]


def test_process_job_gate_blocks_the_provider_without_consent_and_proceeds_with_it(world, monkeypatch):
    db, a, b, c, p, q, live, gone = world
    import jobs.worker as w
    from domain.consent import set_consent
    from sqlalchemy.orm import sessionmaker
    monkeypatch.setenv("HEALTHGUARD_LLM_API_KEY", "test-key")
    monkeypatch.setattr(w, "SessionLocal", sessionmaker(bind=db.get_bind()))
    provider_calls, storage_reads = [], []
    monkeypatch.setattr(w, "get_production_provider", lambda: provider_calls.append(1))
    monkeypatch.setattr(w.storage, "get", lambda key: storage_reads.append(key) or (_ for _ in ()).throw(RuntimeError("past-gate")))
    live.status = DocumentStatus.QUEUED; live.storage_key = f"patients/{p.id}/documents/{live.id}/lab.pdf"      # a correctly scoped key, as production writes
    key = live.storage_key
    job = ProcessingJob(document_id=live.id, status=DocumentStatus.QUEUED, attempts=0); db.add(job); db.commit(); job_id = job.id

    assert w.process_job(job_id) == "FAILED"
    db.expire_all(); j = db.get(ProcessingJob, job_id)
    assert j.status == DocumentStatus.FAILED and j.last_error == w.CONSENT_BLOCKED and j.next_attempt_at is None
    assert not provider_calls and not storage_reads, "no document content may be read or sent before consent"

    set_consent(db, a, "AI_PROCESSING", True); j.status = DocumentStatus.QUEUED; j.last_error = None; live.status = DocumentStatus.QUEUED; db.commit()
    w.process_job(job_id)
    assert storage_reads == [key], "with consent, processing must continue past the gate"
    db.expire_all(); assert db.get(ProcessingJob, job_id).last_error != w.CONSENT_BLOCKED
