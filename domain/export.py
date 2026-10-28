"""Patient data export (data portability).

Builds a self-describing JSON bundle of everything HealthGuard holds about ONE patient, readable without HealthGuard.

Completeness is enforced, not hoped for: EVERY table in the schema must be listed in ``EXPORTED_TABLES`` or in
``NOT_EXPORTED`` with a reason (a test fails when a table is added without a decision). Secrets (password hashes, share-token
hashes, storage keys) are never exported. Content of deleted or deletion-pending documents is never exported, and records that
cited such a document are exported without the citation.
"""
from __future__ import annotations

import enum
import uuid
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import (Document, DocumentStatus, DocumentTextBlock, HealthAction, HealthEvent, HealthFact, Handoff, Investigation, InvestigationResult,
                       Measurement, Medication, MedicationEvent, PatientAccess, PatientProfile, ReminderDelivery, ReminderPreference, TemporaryShare, User, Consent)

EXPORT_VERSION = "healthguard.export.v1"

# table -> export section
EXPORTED_TABLES = {
    "patient_profiles": "patient", "documents": "documents", "document_text_blocks": "document_text", "health_facts": "health_facts",
    "health_events": "health_events", "medications": "medications", "medication_events": "medication_events", "investigations": "investigations",
    "investigation_results": "investigation_results", "measurements": "measurements", "health_actions": "health_actions", "handoffs": "handoffs",
    "patient_access": "access_grants", "temporary_shares": "temporary_shares", "reminder_preferences": "reminder_preferences",
    "reminder_deliveries": "reminder_deliveries", "consents": "consents",
}
# table -> why it is deliberately not part of a patient's export
NOT_EXPORTED = {
    "users": "account credentials and identity belong to the account, not the patient record (passwords/hashes are never exported)",
    "email_verification_tokens": "single-use secrets",
    "processing_jobs": "internal job/queue state with no clinical content",
    "audit_logs": "operational security log shared across users; contains other people's actions",
    "security_events": "operational security telemetry",
    "ops_alerts": "operational alerts about the service, not about the patient",
    "admin_grants": "internal administrator authorisation state",
}

_SECRET_COLUMNS = {"password_hash", "token_hash", "storage_key"}
_DEAD = (DocumentStatus.DELETED, DocumentStatus.DELETION_PENDING)


def _value(v: Any) -> Any:
    if isinstance(v, uuid.UUID): return str(v)
    if isinstance(v, (datetime, date)): return v.isoformat()
    if isinstance(v, enum.Enum): return v.value if not isinstance(v.value, (int, float)) else v.name
    return v


def _row(obj, *, exclude: set[str] = frozenset(), dead_docs: set[uuid.UUID] = frozenset()) -> dict:
    out = {}
    for col in obj.__table__.columns:
        if col.name in _SECRET_COLUMNS or col.name in exclude:
            continue
        v = getattr(obj, col.name)
        if col.name in {"document_id", "source_document_id"} and v in dead_docs:
            v = None                                  # the record is kept; its citation to a deleted file is not
        out[col.name] = _value(v)
    return out


def _mask_email(email: str | None) -> str | None:
    if not email or "@" not in email: return None
    local, domain = email.split("@", 1)
    return f"{local[:1]}***@{domain}"


def build_patient_export(db: Session, patient: PatientProfile, user: User) -> dict:
    pid = patient.id
    all_docs = db.scalars(select(Document).where(Document.patient_id == pid)).all()
    dead = {d.id for d in all_docs if d.status in _DEAD}
    live_docs = [d for d in all_docs if d.id not in dead]
    live_ids = [d.id for d in live_docs]

    def many(model, *clauses, **kw):
        return [_row(r, dead_docs=dead, **kw) for r in db.scalars(select(model).where(*clauses)).all()]

    meds = db.scalars(select(Medication).where(Medication.patient_id == pid)).all()
    invs = db.scalars(select(Investigation).where(Investigation.patient_id == pid)).all()
    grants = db.scalars(select(PatientAccess).where(PatientAccess.patient_id == pid)).all()
    grantee = {u.id: u for u in db.scalars(select(User).where(User.id.in_([g.user_id for g in grants]))).all()} if grants else {}

    sections: dict[str, list | dict] = {
        "patient": _row(patient, exclude={"owner_user_id", "deletion_pending"}),
        "documents": [_row(d, exclude={"uploaded_by"}) for d in live_docs],
        "document_text": ([_row(b) for b in db.scalars(select(DocumentTextBlock).where(DocumentTextBlock.document_id.in_(live_ids))
                                                      .order_by(DocumentTextBlock.document_id, DocumentTextBlock.page_number, DocumentTextBlock.block_index)).all()] if live_ids else []),
        "health_facts": [_row(f, dead_docs=dead) for f in db.scalars(select(HealthFact).where(HealthFact.patient_id == pid)).all() if f.document_id not in dead],
        "health_events": many(HealthEvent, HealthEvent.patient_id == pid, exclude={"created_by"}),
        "medications": [_row(m, exclude={"created_by"}) for m in meds],
        "medication_events": ([_row(e, exclude={"created_by"}) for e in db.scalars(select(MedicationEvent).where(MedicationEvent.medication_id.in_([m.id for m in meds]))).all()] if meds else []),
        "investigations": [_row(i, exclude={"created_by"}) for i in invs],
        "investigation_results": ([_row(r, exclude={"created_by"}) for r in db.scalars(select(InvestigationResult).where(InvestigationResult.investigation_id.in_([i.id for i in invs]))).all()] if invs else []),
        "measurements": many(Measurement, Measurement.patient_id == pid, exclude={"created_by"}),
        "health_actions": many(HealthAction, HealthAction.patient_id == pid),
        "handoffs": many(Handoff, Handoff.patient_id == pid),
        "access_grants": [{**_row(g, exclude={"user_id", "granted_by"}), "grantee_email_masked": _mask_email(getattr(grantee.get(g.user_id), "email", None))} for g in grants],
        "temporary_shares": many(TemporaryShare, TemporaryShare.patient_id == pid, exclude={"created_by"}),
        "reminder_preferences": many(ReminderPreference, ReminderPreference.patient_id == pid, ReminderPreference.user_id == user.id, exclude={"user_id"}),
        "reminder_deliveries": many(ReminderDelivery, ReminderDelivery.patient_id == pid, ReminderDelivery.user_id == user.id, exclude={"user_id", "last_error"}),
        "consents": [_row(c, exclude={"user_id"}) for c in db.scalars(select(Consent).where(Consent.user_id == user.id)).all()],
    }
    return {"schema_version": EXPORT_VERSION, "exported_at": datetime.now(timezone.utc).isoformat(), "patient_id": str(pid),
            "notes": ["Generated by HealthGuard from the patient's own record. Extracted facts are machine-extracted from the uploaded documents and cite their source page and text; "
                      "they are not clinically verified.", "Content of deleted documents is not included.", "Secrets, credentials and other users' identities are never included."],
            "counts": {k: (len(v) if isinstance(v, list) else 1) for k, v in sections.items()}, **sections}


def original_documents(db: Session, patient: PatientProfile) -> list[Document]:
    return [d for d in db.scalars(select(Document).where(Document.patient_id == patient.id)).all() if d.status not in _DEAD]
