from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.auth import get_current_user
from backend.main import app
from db.models import (
    Base, DatePrecision, Document, DocumentStatus, EventSource, EventType, HealthEvent,
    HealthFact, Investigation, InvestigationResult, Medication, MedicationStatus,
    PatientProfile, Provenance, User,
)
from db.session import get_db
from domain.authorization import patient_for_user
from domain.longitudinal import build_health_snapshot, build_longitudinal_timeline, normalize_temporal_value, retrieve_health_facts


def make_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    @event.listens_for(engine, "connect")
    def _fk(conn, _): conn.execute("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def seed(Session):
    db = Session()
    owner = User(username=f"owner-{uuid4().hex[:8]}", email=f"owner-{uuid4().hex[:8]}@example.test", password_hash="x", email_verified=True)
    other = User(username=f"other-{uuid4().hex[:8]}", email=f"other-{uuid4().hex[:8]}@example.test", password_hash="x", email_verified=True)
    db.add_all([owner, other]); db.flush()
    patient = PatientProfile(owner_user_id=owner.id, first_name="Owner", last_name="Patient")
    other_patient = PatientProfile(owner_user_id=other.id, first_name="Other", last_name="Patient")
    db.add_all([patient, other_patient]); db.flush()
    return db, owner, other, patient, other_patient


def add_doc(db, patient, owner, name="record.pdf"):
    doc = Document(
        patient_id=patient.id, uploaded_by=owner.id, filename=name, content_type="application/pdf", file_size=10,
        storage_key=f"patients/{patient.id}/documents/{uuid4()}/{name}", checksum=uuid4().hex + "0" * 32,
        status=DocumentStatus.COMPLETED,
    )
    db.add(doc); db.flush(); return doc


def add_fact(db, patient, doc, *, value, fact_type="LAB_RESULT", when=None, precision=DatePrecision.UNKNOWN, source_date_text=None, normalized=None, confidence=.9):
    fact = HealthFact(
        patient_id=patient.id, document_id=doc.id, fact_type=fact_type, value=value, normalized_value=normalized,
        temporal_date=when, temporal_precision=precision, source_date_text=source_date_text,
        source_page=2, source_text=f"Evidence: {value}", confidence=confidence, extraction_method="TEST", provenance=Provenance.DOCUMENT_VERIFIED,
    )
    db.add(fact); db.flush(); return fact


def test_temporal_normalization_supports_exact_approximate_year_unknown_without_invention():
    assert normalize_temporal_value(date(2026, 4, 3), DatePrecision.EXACT, "03 Apr 2026").precision is DatePrecision.EXACT
    assert normalize_temporal_value(date(2026, 4, 1), DatePrecision.APPROXIMATE, "April 2026").value == date(2026, 4, 1)
    assert normalize_temporal_value(date(2021, 1, 1), DatePrecision.YEAR, "2021").value == date(2021, 1, 1)
    unknown = normalize_temporal_value(None, DatePrecision.UNKNOWN, "date not stated")
    assert unknown.value is None and unknown.precision is DatePrecision.UNKNOWN


def test_temporal_normalization_rejects_implicit_dates():
    import pytest
    with pytest.raises(ValueError): normalize_temporal_value(None, DatePrecision.YEAR)
    with pytest.raises(ValueError): normalize_temporal_value(date(2026, 1, 2), DatePrecision.YEAR)
    with pytest.raises(ValueError): normalize_temporal_value(date(2026, 1, 2), DatePrecision.UNKNOWN)


def test_retrieval_filters_and_orders_patient_facts():
    Session = make_db(); db, owner, _, patient, _ = seed(Session); doc = add_doc(db, patient, owner)
    add_fact(db, patient, doc, value="Glucose: 100", when=date(2025, 1, 1), precision=DatePrecision.EXACT, normalized="glucose: 100")
    add_fact(db, patient, doc, value="Glucose: 110", when=date(2026, 1, 1), precision=DatePrecision.YEAR, source_date_text="2026", normalized="glucose: 110")
    add_fact(db, patient, doc, value="Unrelated", fact_type="OTHER", confidence=.4)
    db.commit()
    rows = retrieve_health_facts(db, patient.id, fact_type="LAB_RESULT", min_confidence=.8)
    assert [r.value for r in rows] == ["Glucose: 110", "Glucose: 100"]
    assert rows[1].source_date_text is None
    db.close()


def test_timeline_is_chronological_and_preserves_evidence():
    Session = make_db(); db, owner, _, patient, _ = seed(Session); doc = add_doc(db, patient, owner)
    fact = add_fact(db, patient, doc, value="Hemoglobin: 13.2", when=date(2024, 5, 2), precision=DatePrecision.EXACT, source_date_text="02 May 2024", normalized="hemoglobin: 13.2")
    db.add(HealthEvent(patient_id=patient.id, event_type=EventType.SURGERY, event_date=date(2022, 3, 1), date_precision=DatePrecision.EXACT, title="Surgery", provenance=Provenance.USER_REPORTED, source_type=EventSource.MANUAL, created_by=owner.id))
    db.commit()
    items = build_longitudinal_timeline(db, patient.id)
    assert [x["date"] for x in items] == sorted([x["date"] for x in items], key=lambda d: d or date.max)
    fact_item = next(x for x in items if x["id"] == str(fact.id))
    assert fact_item["evidence"]["document_id"] == str(doc.id)
    assert fact_item["evidence"]["page"] == 2
    assert fact_item["evidence"]["text"] == "Evidence: Hemoglobin: 13.2"
    db.close()


def test_duplicate_and_contradictory_facts_are_marked_without_overwriting():
    Session = make_db(); db, owner, _, patient, _ = seed(Session)
    d1 = add_doc(db, patient, owner, "a.pdf"); d2 = add_doc(db, patient, owner, "b.pdf")
    first = add_fact(db, patient, d1, value="Hemoglobin: 13.2", normalized="hemoglobin: 13.2")
    duplicate = add_fact(db, patient, d2, value="Hemoglobin: 13.2", normalized="hemoglobin: 13.2")
    conflict = add_fact(db, patient, d2, value="Hemoglobin: 10.1", normalized="hemoglobin: 10.1")
    db.commit()
    items = build_longitudinal_timeline(db, patient.id)
    by_id = {x["id"]: x for x in items}
    assert by_id[str(first.id)]["relation"] == "DUPLICATE"
    assert by_id[str(duplicate.id)]["relation"] == "DUPLICATE"
    assert by_id[str(conflict.id)]["relation"] == "CONTRADICTORY"
    assert db.get(HealthFact, first.id) is not None and db.get(HealthFact, duplicate.id) is not None and db.get(HealthFact, conflict.id) is not None
    db.close()


def test_snapshot_contains_only_supported_evidence_backed_records():
    Session = make_db(); db, owner, _, patient, _ = seed(Session); doc = add_doc(db, patient, owner)
    db.add(Medication(patient_id=patient.id, medicine_name="Metformin", status=MedicationStatus.ACTIVE, provenance=Provenance.USER_REPORTED, created_by=owner.id))
    db.add(HealthEvent(patient_id=patient.id, event_type=EventType.DIAGNOSIS_DOCUMENTED, event_date=date(2025,1,1), date_precision=DatePrecision.EXACT, title="Documented condition", description="As recorded", provenance=Provenance.USER_REPORTED, source_type=EventSource.MANUAL, created_by=owner.id))
    add_fact(db, patient, doc, value="Type 2 diabetes", fact_type="DIAGNOSIS", when=date(2025,1,1), precision=DatePrecision.EXACT, normalized="type 2 diabetes")
    add_fact(db, patient, doc, value="COVID-19 vaccine", fact_type="VACCINATION", when=date(2024,1,1), precision=DatePrecision.YEAR, normalized="covid-19 vaccine")
    add_fact(db, patient, doc, value="Maybe severe", fact_type="SEVERITY", when=date(2024,1,1), precision=DatePrecision.YEAR)
    db.commit()
    snapshot = build_health_snapshot(db, patient.id)
    assert [m["title"] for m in snapshot["medications"]] == ["Metformin"]
    assert snapshot["diagnoses_conditions"][0]["value"] == "Type 2 diabetes"
    assert snapshot["vaccinations"][0]["value"] == "COVID-19 vaccine"
    assert not any(x["value"] == "Maybe severe" for x in snapshot["diagnoses_conditions"])
    db.close()


def test_cross_patient_isolation_for_service_and_endpoint():
    Session = make_db(); db, owner, other, patient, other_patient = seed(Session)
    d = add_doc(db, other_patient, other); add_fact(db, other_patient, d, value="secret", normalized="secret"); db.commit()
    assert retrieve_health_facts(db, patient.id) == []
    try:
        patient_for_user(db, owner, other_patient.id)
        assert False, "cross-user authorization unexpectedly allowed"
    except HTTPException as exc:
        assert exc.status_code == 403

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: owner
    client = TestClient(app)
    response = client.get(f"/api/patients/{other_patient.id}/health-facts")
    assert response.status_code == 403
    response = client.get(f"/api/patients/{other_patient.id}/timeline")
    assert response.status_code == 403
    response = client.get(f"/api/patients/{other_patient.id}/health-snapshot")
    assert response.status_code == 403
    app.dependency_overrides.clear()
    db.close()


def test_timeline_includes_medication_and_investigation_history_with_provenance():
    Session = make_db(); db, owner, _, patient, _ = seed(Session)
    med = Medication(patient_id=patient.id, medicine_name="Amoxicillin", start_date=date(2025,1,2), status=MedicationStatus.ACTIVE, provenance=Provenance.USER_REPORTED, created_by=owner.id)
    inv = Investigation(patient_id=patient.id, name="CBC", performed_date=date(2025,1,3), provenance=Provenance.USER_REPORTED, created_by=owner.id)
    db.add_all([med, inv]); db.flush()
    db.add(InvestigationResult(investigation_id=inv.id, result_summary="Recorded result", result_date=date(2025,1,4), created_by=owner.id))
    db.commit()
    items = build_longitudinal_timeline(db, patient.id)
    kinds = {x["item_type"] for x in items}
    assert {"MEDICATION", "INVESTIGATION", "INVESTIGATION_RESULT"}.issubset(kinds)
    assert all("provenance" in x and "evidence" in x for x in items)
    db.close()


def test_measurement_concepts_ignore_numeric_values_and_units_for_conflicts():
    Session = make_db(); db, owner, _, patient, _ = seed(Session)
    d1 = add_doc(db, patient, owner, "lab-a.pdf"); d2 = add_doc(db, patient, owner, "lab-b.pdf")
    h1 = add_fact(db, patient, d1, value="Hemoglobin 13.2 g/dL", normalized="hemoglobin 13.2 g/dL")
    h2 = add_fact(db, patient, d2, value="Hemoglobin 10.1 g/dL", normalized="hemoglobin 10.1 g/dL")
    g1 = add_fact(db, patient, d1, value="Glucose 100 mg/dL", normalized="glucose 100 mg/dL")
    g2 = add_fact(db, patient, d2, value="Glucose 110 mg/dL", normalized="glucose 110 mg/dL")
    unrelated = add_fact(db, patient, d2, value="Platelets 110 10^9/L", normalized="platelets 110 10^9/l")
    db.commit()
    by_id = {x["id"]: x for x in build_longitudinal_timeline(db, patient.id)}
    assert by_id[str(h1.id)]["relation"] == "CONTRADICTORY"
    assert by_id[str(h2.id)]["relation"] == "CONTRADICTORY"
    assert by_id[str(g1.id)]["relation"] == "CONTRADICTORY"
    assert by_id[str(g2.id)]["relation"] == "CONTRADICTORY"
    assert by_id[str(unrelated.id)]["relation"] == "UNIQUE"
    assert db.get(HealthFact, h1.id) is not None and db.get(HealthFact, h2.id) is not None
    db.close()


def test_timeline_and_snapshot_preserve_provenance_without_upgrading_it():
    Session = make_db(); db, owner, _, patient, _ = seed(Session); doc = add_doc(db, patient, owner)
    document_fact = add_fact(db, patient, doc, value="Hemoglobin 13.2 g/dL", fact_type="LAB_RESULT")
    user_fact = add_fact(db, patient, doc, value="Asthma", fact_type="CONDITION")
    user_fact.provenance = Provenance.USER_REPORTED
    family_fact = add_fact(db, patient, doc, value="Flu vaccine", fact_type="VACCINATION")
    family_fact.provenance = Provenance.FAMILY_REPORTED
    system_fact = add_fact(db, patient, doc, value="derived marker", fact_type="SYSTEM_MARKER")
    system_fact.provenance = Provenance.SYSTEM_DERIVED
    db.commit()
    timeline = build_longitudinal_timeline(db, patient.id)
    by_id = {x["id"]: x for x in timeline}
    assert by_id[str(document_fact.id)]["provenance"] == Provenance.DOCUMENT_VERIFIED.value
    assert by_id[str(user_fact.id)]["provenance"] == Provenance.USER_REPORTED.value
    assert by_id[str(family_fact.id)]["provenance"] == Provenance.FAMILY_REPORTED.value
    assert by_id[str(system_fact.id)]["provenance"] == Provenance.SYSTEM_DERIVED.value
    snapshot = build_health_snapshot(db, patient.id)
    snap = {x["id"]: x for section in ("diagnoses_conditions", "vaccinations", "document_facts") for x in snapshot[section]}
    assert snap[str(user_fact.id)]["provenance"] == Provenance.USER_REPORTED.value
    assert snap[str(family_fact.id)]["provenance"] == Provenance.FAMILY_REPORTED.value
    assert snap[str(document_fact.id)]["provenance"] == Provenance.DOCUMENT_VERIFIED.value
    assert snap[str(system_fact.id)]["provenance"] == Provenance.SYSTEM_DERIVED.value
    db.close()
