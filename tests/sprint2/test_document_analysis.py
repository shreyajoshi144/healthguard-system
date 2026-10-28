from __future__ import annotations

from pathlib import Path
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from db.models import Base, Document, DocumentStatus, DocumentTextBlock, HealthFact, PatientProfile, ProcessingJob, User, Provenance
from jobs.worker import process_job
from jobs.extractors import ExtractedBlock
from storage.service import Storage
from ai.document_provider import ClassificationResult, ExtractedFact
from backend.main import app
from backend.auth import get_current_user
from db.session import get_db


class FakeMedicalProvider:
    def classify(self, text_blocks):
        text = " ".join(b["source_text"] for b in text_blocks).lower()
        if "hemoglobin" in text or "glucose" in text:
            return ClassificationResult("LAB_REPORT", 0.99)
        if "rx:" in text or "take" in text:
            return ClassificationResult("PRESCRIPTION", 0.98)
        return ClassificationResult("OTHER", 0.7)

    def extract_facts(self, text_blocks, document_type):
        text = " ".join(b["source_text"] for b in text_blocks)
        page = text_blocks[0]["page_number"]
        if document_type == "LAB_REPORT":
            facts = []
            if "Hemoglobin" in text:
                facts.append(ExtractedFact("LAB_RESULT", "Hemoglobin: 13.2 g/dL", "13.2 g/dL", page, "Hemoglobin: 13.2 g/dL", 0.94, "FAKE_TEST"))
            # Deliberately do not emit an unsupported glucose value.
            return facts
        if document_type == "PRESCRIPTION":
            return [ExtractedFact("MEDICATION_PRESCRIPTION", "Amoxicillin 500 mg", "amoxicillin 500 mg", page, "Rx: Amoxicillin 500 mg", 0.62, "FAKE_TEST")]
        return []


class LowConfidenceProvider(FakeMedicalProvider):
    def extract_facts(self, text_blocks, document_type):
        return [ExtractedFact("LAB_RESULT", "Hemoglobin: 13.2 g/dL", "13.2 g/dL", 1, "Hemoglobin: 13.2 g/dL", 0.31, "FAKE_TEST")]


def make_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    @event.listens_for(engine, "connect")
    def _fk(conn, _): conn.execute("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def seed(Session, storage_root: Path, content: bytes, filename="record.txt"):
    db = Session()
    user = User(username="analysis-owner", email="analysis-owner@example.test", password_hash="x", email_verified=True)
    db.add(user); db.flush()
    patient = PatientProfile(owner_user_id=user.id, first_name="Analysis", last_name="Patient")
    db.add(patient); db.flush()
    doc = Document(patient_id=patient.id, uploaded_by=user.id, filename=filename, content_type="text/plain", file_size=len(content), storage_key="pending", checksum=("b"*64), status=DocumentStatus.QUEUED)
    db.add(doc); db.flush()
    doc.storage_key=f"patients/{patient.id}/documents/{doc.id}/{filename}"
    job=ProcessingJob(document_id=doc.id,status=DocumentStatus.QUEUED)
    db.add(job); db.commit()
    storage=Storage(); storage.backend="local"; storage.root=storage_root; storage.put(doc.storage_key, content, "text/plain")
    return db,user,patient,doc,job,storage


def install_fake(monkeypatch, provider):
    monkeypatch.setattr("jobs.worker.get_production_provider", lambda: provider)


def test_lab_report_extracts_only_explicit_fact_and_preserves_evidence(monkeypatch, tmp_path):
    Session=make_db(); db,user,patient,doc,job,storage=seed(Session,tmp_path,b"LAB REPORT\nHemoglobin: 13.2 g/dL\nGlucose: not reported")
    # Use a deterministic text block instead of the real extractor.
    db.add(DocumentTextBlock(document_id=doc.id,page_number=2,block_index=0,source_text="Hemoglobin: 13.2 g/dL",extraction_method="PDF_TEXT",confidence=1.0)); db.commit()
    install_fake(monkeypatch, FakeMedicalProvider()); monkeypatch.setattr("jobs.worker.storage", storage)
    # The existing extractor does not support text/plain, so seed evidence and call analysis directly.
    from domain.document_analysis import analyze_document
    facts=analyze_document(db,doc,FakeMedicalProvider()); db.commit()
    assert len(facts)==1
    fact=facts[0]
    assert fact.patient_id==patient.id and fact.document_id==doc.id
    assert fact.source_page==2
    assert fact.source_text=="Hemoglobin: 13.2 g/dL"
    assert fact.provenance is Provenance.DOCUMENT_VERIFIED
    assert not any("glucose" in f.value.lower() for f in db.scalars(select(HealthFact)).all())
    db.close()


def test_prescription_is_extracted_without_claiming_medication_started(monkeypatch, tmp_path):
    Session=make_db(); db,user,patient,doc,job,storage=seed(Session,tmp_path,b"Prescription\nRx: Amoxicillin 500 mg\nTake twice daily")
    db.add(DocumentTextBlock(document_id=doc.id,page_number=1,block_index=0,source_text="Rx: Amoxicillin 500 mg",extraction_method="PDF_TEXT",confidence=1.0)); db.commit()
    from domain.document_analysis import analyze_document
    facts=analyze_document(db,doc,FakeMedicalProvider()); db.commit()
    assert facts[0].fact_type=="MEDICATION_PRESCRIPTION"
    assert facts[0].confidence==0.62
    from db.models import MedicationEvent
    assert db.scalars(select(MedicationEvent)).all()==[]
    db.close()


def test_source_page_and_text_are_required_evidence(monkeypatch, tmp_path):
    Session=make_db(); db,user,patient,doc,job,storage=seed(Session,tmp_path,b"lab")
    db.add(DocumentTextBlock(document_id=doc.id,page_number=3,block_index=0,source_text="Hemoglobin: 13.2 g/dL",extraction_method="PDF_TEXT",confidence=1.0)); db.commit()
    from domain.document_analysis import analyze_document
    facts=analyze_document(db,doc,FakeMedicalProvider()); db.commit()
    assert facts[0].source_page==3 and facts[0].source_text in "Hemoglobin: 13.2 g/dL"
    db.close()


def test_provenance_cannot_be_client_controlled(monkeypatch):
    Session=make_db(); db=Session(); db.close()
    # There is intentionally no POST HealthFact endpoint; clients cannot supply provenance.
    assert any(r.path=="/api/patients/{patient_id}/health-facts" and "POST" not in r.methods for r in app.routes)


def test_low_confidence_is_retained_as_uncertain(monkeypatch, tmp_path):
    Session=make_db(); db,user,patient,doc,job,storage=seed(Session,tmp_path,b"lab")
    db.add(DocumentTextBlock(document_id=doc.id,page_number=1,block_index=0,source_text="Hemoglobin: 13.2 g/dL",extraction_method="PDF_TEXT",confidence=1.0)); db.commit()
    from domain.document_analysis import analyze_document
    fact=analyze_document(db,doc,LowConfidenceProvider())[0]; db.commit()
    assert fact.confidence==0.31
    assert fact.provenance is Provenance.DOCUMENT_VERIFIED
    db.close()


def test_retry_does_not_duplicate_health_facts(monkeypatch, tmp_path):
    Session=make_db(); db,user,patient,doc,job,storage=seed(Session,tmp_path,b"lab")
    db.add(DocumentTextBlock(document_id=doc.id,page_number=1,block_index=0,source_text="Hemoglobin: 13.2 g/dL",extraction_method="PDF_TEXT",confidence=1.0)); db.commit()
    from domain.document_analysis import analyze_document
    analyze_document(db,doc,FakeMedicalProvider()); db.commit()
    first=list(db.scalars(select(HealthFact).where(HealthFact.document_id==doc.id)).all())
    analyze_document(db,doc,FakeMedicalProvider()); db.commit()
    second=list(db.scalars(select(HealthFact).where(HealthFact.document_id==doc.id)).all())
    assert len(first)==len(second)==1
    assert len({(f.fact_type,f.value,f.source_page,f.source_text) for f in second})==1
    db.close()


def test_user_a_cannot_access_user_b_health_facts():
    Session=make_db(); db=Session()
    a=User(username="user-a",email="a@example.test",password_hash="x",email_verified=True); b=User(username="user-b",email="b@example.test",password_hash="x",email_verified=True); db.add_all([a,b]); db.flush()
    pa=PatientProfile(owner_user_id=a.id,first_name="A",last_name="Patient"); pb=PatientProfile(owner_user_id=b.id,first_name="B",last_name="Patient"); db.add_all([pa,pb]); db.flush()
    doc=Document(patient_id=pb.id,uploaded_by=b.id,filename="b.txt",content_type="text/plain",file_size=1,storage_key="x",checksum="c"*64,status=DocumentStatus.COMPLETED); db.add(doc); db.flush()
    fact=HealthFact(patient_id=pb.id,document_id=doc.id,fact_type="LAB_RESULT",value="secret",source_page=1,source_text="secret",confidence=.9,extraction_method="FAKE_TEST",provenance=Provenance.DOCUMENT_VERIFIED); db.add(fact); db.commit()
    from domain.authorization import patient_for_user
    from fastapi import HTTPException
    try:
        patient_for_user(db,a,pb.id)
        assert False, "cross-user access unexpectedly allowed"
    except HTTPException as exc:
        assert exc.status_code==403
    db.close()


def test_worker_retry_does_not_duplicate_health_facts(monkeypatch, tmp_path):
    Session=make_db(); db,user,patient,doc,job,storage=seed(Session,tmp_path,b"lab")
    db.add(DocumentTextBlock(document_id=doc.id,page_number=1,block_index=0,source_text="Hemoglobin: 13.2 g/dL",extraction_method="PDF_TEXT",confidence=1.0));
    job.status=DocumentStatus.FAILED; doc.status=DocumentStatus.FAILED; db.commit()
    monkeypatch.setattr("jobs.worker.storage", storage)
    monkeypatch.setattr("jobs.worker.SessionLocal", Session)
    monkeypatch.setattr("jobs.worker.get_production_provider", lambda: FakeMedicalProvider())
    monkeypatch.setattr("jobs.worker.extractor.extract", lambda *_args: [ExtractedBlock(1, "Hemoglobin: 13.2 g/dL", "PDF_TEXT", 1.0, 0)])
    # Requeue exactly as the existing retry boundary does, then execute the worker twice.
    job.status=DocumentStatus.QUEUED; doc.status=DocumentStatus.QUEUED; db.commit()
    assert process_job(job.id)=="READY"
    first=list(db.scalars(select(HealthFact).where(HealthFact.document_id==doc.id)).all())
    assert len(first)==1
    assert process_job(job.id)=="ALREADY_READY"
    second=list(db.scalars(select(HealthFact).where(HealthFact.document_id==doc.id)).all())
    assert len(second)==1
    db.close()
