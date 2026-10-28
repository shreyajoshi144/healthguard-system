from __future__ import annotations

from datetime import date, datetime, timezone
from uuid import UUID
from sqlalchemy import select
from sqlalchemy.orm import Session
from db.models import (HealthAction, HealthActionSource, HealthActionStatus, DatePrecision, Provenance,
                        Document, DocumentStatus, HealthFact, HealthEvent, EventSource, Investigation,
                        InvestigationResult, Medication, MedicationEvent)


def utcnow():
    return datetime.now(timezone.utc)



def validate_document_derived_sources(db: Session, *, patient_id: UUID,
                                      source_document_id: UUID | None = None,
                                      source_fact_id: UUID | None = None,
                                      source_event_id: UUID | None = None,
                                      source_investigation_id: UUID | None = None,
                                      source_medication_event_id: UUID | None = None) -> None:
    """Require every supplied DOCUMENT_DERIVED reference to be genuinely document-backed.

    Patient ownership is checked here as a second boundary. Provenance is never upgraded
    merely because the client selected DOCUMENT_DERIVED.
    """
    def document_verified(document_id: UUID | None) -> bool:
        if document_id is None:
            return False
        return db.scalar(select(Document.id).where(
            Document.id == document_id,
            Document.patient_id == patient_id,
            Document.status == DocumentStatus.COMPLETED,
        )) is not None

    if source_document_id is not None and not document_verified(source_document_id):
        raise ValueError("The source document must be completed and document-backed.")

    if source_fact_id is not None:
        fact = db.scalar(select(HealthFact).where(HealthFact.id == source_fact_id, HealthFact.patient_id == patient_id))
        if fact is None:
            raise ValueError("The source health fact does not belong to this patient.")
        if fact.provenance != Provenance.DOCUMENT_VERIFIED or not document_verified(fact.document_id):
            raise ValueError("The source health fact is not document-verified.")

    if source_event_id is not None:
        event = db.scalar(select(HealthEvent).where(HealthEvent.id == source_event_id, HealthEvent.patient_id == patient_id))
        if event is None:
            raise ValueError("The source timeline event does not belong to this patient.")
        if event.provenance != Provenance.DOCUMENT_VERIFIED or event.source_type != EventSource.DOCUMENT or not document_verified(event.source_id):
            raise ValueError("The source timeline event is not document-verified.")

    if source_investigation_id is not None:
        investigation = db.scalar(select(Investigation).where(Investigation.id == source_investigation_id, Investigation.patient_id == patient_id))
        if investigation is None:
            raise ValueError("The source investigation does not belong to this patient.")
        if investigation.provenance != Provenance.DOCUMENT_VERIFIED:
            raise ValueError("The source investigation is not document-verified.")

    if source_medication_event_id is not None:
        event = db.scalar(select(MedicationEvent).join(Medication, Medication.id == MedicationEvent.medication_id).where(
            MedicationEvent.id == source_medication_event_id, Medication.patient_id == patient_id))
        if event is None:
            raise ValueError("The source medication event does not belong to this patient.")
        medication = event.medication
        if event.provenance != Provenance.DOCUMENT_VERIFIED or medication.provenance != Provenance.DOCUMENT_VERIFIED or not document_verified(medication.source_id):
            raise ValueError("The source medication event is not document-verified.")

    if source_fact_id is not None:
        fact = db.scalar(select(HealthFact).where(HealthFact.id == source_fact_id, HealthFact.patient_id == patient_id))
        if fact is not None and source_document_id is not None and fact.document_id != source_document_id:
            raise ValueError("Source references must point to the same document-backed record.")

    if source_investigation_id is not None and source_medication_event_id is not None:
        # Multiple references are allowed, but each must independently pass the checks above.
        pass

def validate_due_date(*, due_date: date | None, temporal_precision: DatePrecision, source: HealthActionSource):
    if due_date is None and temporal_precision != DatePrecision.UNKNOWN:
        raise ValueError("A non-UNKNOWN temporal precision requires a documented or confirmed date.")
    if source == HealthActionSource.DOCUMENT_DERIVED and temporal_precision == DatePrecision.UNKNOWN and due_date is not None:
        raise ValueError("Document-derived actions require explicit temporal precision.")


def create_health_action(db: Session, *, patient_id: UUID, title: str, description: str | None,
                         action_type: str, source: HealthActionSource, provenance: Provenance,
                         due_date: date | None, temporal_precision: DatePrecision,
                         source_document_id: UUID | None = None, source_fact_id: UUID | None = None,
                         source_event_id: UUID | None = None, source_investigation_id: UUID | None = None,
                         source_medication_event_id: UUID | None = None) -> HealthAction:
    validate_due_date(due_date=due_date, temporal_precision=temporal_precision, source=source)
    action = HealthAction(patient_id=patient_id, title=title, description=description, action_type=action_type,
                          source=source, provenance=provenance, status=HealthActionStatus.PLANNED,
                          due_date=due_date, temporal_precision=temporal_precision,
                          source_document_id=source_document_id, source_fact_id=source_fact_id,
                          source_event_id=source_event_id, source_investigation_id=source_investigation_id,
                          source_medication_event_id=source_medication_event_id)
    db.add(action); db.flush(); return action
