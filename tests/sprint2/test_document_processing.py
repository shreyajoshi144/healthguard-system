from __future__ import annotations

from io import BytesIO
from pathlib import Path
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.models import Base, Document, DocumentStatus, DocumentTextBlock, PatientProfile, ProcessingJob, User
from jobs.extractors import DocumentExtractor, ExtractionError
from jobs.worker import process_job
from storage.service import Storage

from ai.document_provider import ClassificationResult, ExtractedFact

class Phase1FakeProvider:
    def classify(self, text_blocks):
        return ClassificationResult("OTHER", 1.0)
    def extract_facts(self, text_blocks, document_type):
        return []


def make_pdf(text: str = "HealthGuard Sprint 2") -> bytes:
    # Tiny self-contained PDF with one text page; avoids a test dependency on a PDF generator.
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        None,
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects[3] = b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out.extend(f"{index} 0 obj\n".encode())
        out.extend(body)
        out.extend(b"\nendobj\n")
    xref = len(out)
    out.extend(f"xref\n0 {len(objects)+1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        out.extend(f"{offset:010d} 00000 n \n".encode())
    out.extend(f"trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(out)


def make_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    @event.listens_for(engine, "connect")
    def _fk(conn, _): conn.execute("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def seed(session, storage_root: Path, *, content: bytes, content_type="application/pdf"):
    user = User(username="worker-owner", email="worker@example.com", password_hash="x", email_verified=True)
    session.add(user); session.flush()
    patient = PatientProfile(owner_user_id=user.id, first_name="Worker", last_name="Patient")
    session.add(patient); session.flush()
    document = Document(patient_id=patient.id, uploaded_by=user.id, filename="record.pdf", content_type=content_type,
                        file_size=len(content), storage_key=f"patients/{patient.id}/documents/PLACEHOLDER/record.pdf",
                        checksum="a" * 64, status=DocumentStatus.QUEUED)
    session.add(document); session.flush()
    document.storage_key=f"patients/{patient.id}/documents/{document.id}/record.pdf"
    job = ProcessingJob(document_id=document.id, status=DocumentStatus.QUEUED)
    session.add(job); session.commit()
    storage = Storage(); storage.backend="local"; storage.root=storage_root
    storage.put(document.storage_key, content, content_type)
    return user, patient, document, job, storage


def test_pdf_worker_persists_page_provenance_and_is_idempotent(monkeypatch, tmp_path):
    Session = make_db(); db = Session()
    _, patient, document, job, storage = seed(db, tmp_path, content=make_pdf("Patient history"))
    monkeypatch.setattr("jobs.worker.SessionLocal", Session)
    monkeypatch.setattr("jobs.worker.storage", storage)
    monkeypatch.setattr("jobs.worker.get_production_provider", lambda: Phase1FakeProvider())

    assert process_job(job.id) == "READY"
    db.expire_all()
    refreshed = db.get(ProcessingJob, job.id); doc = db.get(Document, document.id)
    blocks = list(db.scalars(select(DocumentTextBlock).where(DocumentTextBlock.document_id == document.id)).all())
    assert refreshed.status is DocumentStatus.COMPLETED
    assert doc.status is DocumentStatus.COMPLETED
    assert refreshed.attempts == 1
    assert len(blocks) == 1
    assert blocks[0].page_number == 1
    assert blocks[0].extraction_method == "PDF_TEXT"
    assert "Patient history" in blocks[0].source_text
    assert blocks[0].document_id == patient.documents[0].id

    assert process_job(job.id) == "ALREADY_READY"
    assert len(list(db.scalars(select(DocumentTextBlock).where(DocumentTextBlock.document_id == document.id)).all())) == 1
    db.close()


def test_failed_processing_never_becomes_ready(monkeypatch, tmp_path):
    Session = make_db(); db = Session()
    _, _, document, job, storage = seed(db, tmp_path, content=b"not a supported PDF", content_type="application/pdf")
    monkeypatch.setattr("jobs.worker.SessionLocal", Session)
    monkeypatch.setattr("jobs.worker.storage", storage)
    monkeypatch.setattr("jobs.worker.get_production_provider", lambda: Phase1FakeProvider())
    monkeypatch.setattr("jobs.worker.MAX_ATTEMPTS", 1)
    # Corrupt/malformed PDF must produce a terminal FAILED state with no evidence.
    assert process_job(job.id) == "FAILED"
    db.expire_all()
    assert db.get(ProcessingJob, job.id).status is DocumentStatus.FAILED
    assert db.get(Document, document.id).status is DocumentStatus.FAILED
    assert list(db.scalars(select(DocumentTextBlock).where(DocumentTextBlock.document_id == document.id)).all()) == []
    db.close()


def test_ocr_abstraction_extracts_image_text():
    from PIL import Image, ImageDraw, ImageFont
    image = Image.new("RGB", (1200, 260), "white")
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 72)
    ImageDraw.Draw(image).text((40, 70), "HEALTHGUARD", font=font, fill="black")
    image_bytes = BytesIO(); image.save(image_bytes, format="PNG"); image_bytes.seek(0)
    block = DocumentExtractor().extract(image_bytes, "image/png")[0]
    assert block.page_number == 1
    assert block.extraction_method == "OCR"
    assert "healthguard" in block.source_text.lower()
    assert block.confidence is None or 0 <= block.confidence <= 1
