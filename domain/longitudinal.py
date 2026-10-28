from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import Select, and_, or_, select
from sqlalchemy.orm import Session

from db.models import (
    DatePrecision,
    Document,
    HealthEvent,
    HealthFact,
    Investigation,
    InvestigationResult,
    Medication,
    MedicationEvent,
)


@dataclass(frozen=True)
class TemporalValue:
    value: date | None
    precision: DatePrecision
    source_date_text: str | None = None


def normalize_temporal_value(
    value: date | None,
    precision: DatePrecision,
    source_date_text: str | None = None,
) -> TemporalValue:
    """Validate an already-explicit date without inventing one."""
    if value is None and precision is not DatePrecision.UNKNOWN:
        raise ValueError("A known temporal precision requires an explicit date value.")
    if value is not None and precision is DatePrecision.UNKNOWN:
        raise ValueError("An explicit date value cannot have UNKNOWN precision.")
    if precision is DatePrecision.YEAR and value is not None and (value.month != 1 or value.day != 1):
        raise ValueError("YEAR precision must use January 1 as the stored date marker.")
    return TemporalValue(value=value, precision=precision, source_date_text=source_date_text)


def _fact_query(
    patient_id: UUID,
    *,
    fact_type: str | None = None,
    document_id: UUID | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    min_confidence: float | None = None,
) -> Select:
    conditions: list[Any] = [HealthFact.patient_id == patient_id]
    if fact_type:
        conditions.append(HealthFact.fact_type == fact_type)
    if document_id:
        conditions.append(HealthFact.document_id == document_id)
    if start_date:
        conditions.append(HealthFact.temporal_date >= start_date)
    if end_date:
        conditions.append(HealthFact.temporal_date <= end_date)
    if min_confidence is not None:
        conditions.append(HealthFact.confidence >= min_confidence)
    return select(HealthFact).where(and_(*conditions)).order_by(
        HealthFact.temporal_date.desc().nullslast(),
        HealthFact.created_at.desc(),
        HealthFact.id.desc(),
    )


def retrieve_health_facts(
    db: Session,
    patient_id: UUID,
    *,
    fact_type: str | None = None,
    document_id: UUID | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    min_confidence: float | None = None,
    page: int = 1,
    page_size: int = 50,
) -> list[HealthFact]:
    stmt = _fact_query(
        patient_id,
        fact_type=fact_type,
        document_id=document_id,
        start_date=start_date,
        end_date=end_date,
        min_confidence=min_confidence,
    ).offset((page - 1) * page_size).limit(page_size)
    return list(db.scalars(stmt).all())


_MEASUREMENT_START = re.compile(r"\s+(?=[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:\s|$))")


def _fact_concept(fact: HealthFact) -> str:
    """Return a conservative medical concept key without discarding the fact value.

    Labels followed by measurements such as ``Hemoglobin 13.2 g/dL`` are
    compared by their label only. Numeric/alphanumeric medical names such as
    ``Vitamin B12`` are left intact unless the number begins a measurement
    token. This is deliberately heuristic: it identifies candidate peers but
    never resolves a clinical conflict.
    """
    value = (fact.normalized_value or fact.value).strip().lower()
    if ":" in value:
        return value.split(":", 1)[0].strip()
    match = _MEASUREMENT_START.search(value)
    if match:
        concept = value[:match.start()].strip(" :,-")
        if concept:
            return concept
    return value


def _relation_for_fact(fact: HealthFact, all_facts: list[HealthFact]) -> tuple[str, list[str]]:
    peers = [
        other
        for other in all_facts
        if other.id != fact.id and other.fact_type == fact.fact_type and _fact_concept(other) == _fact_concept(fact)
    ]
    if not peers:
        return "UNIQUE", []
    same = [
        other for other in peers
        if (other.normalized_value or other.value).strip().lower() == (fact.normalized_value or fact.value).strip().lower()
    ]
    if same:
        return "DUPLICATE", [str(x.id) for x in same]
    return "CONTRADICTORY", [str(x.id) for x in peers]


def _fact_item(fact: HealthFact, all_facts: list[HealthFact]) -> dict[str, Any]:
    relation, related = _relation_for_fact(fact, all_facts)
    return {
        "id": str(fact.id),
        "patient_id": str(fact.patient_id),
        "document_id": str(fact.document_id),
        "fact_type": fact.fact_type,
        "value": fact.value,
        "normalized_value": fact.normalized_value,
        "temporal_date": fact.temporal_date,
        "temporal_precision": fact.temporal_precision.value,
        "source_date_text": fact.source_date_text,
        "source_page": fact.source_page,
        "source_text": fact.source_text,
        "confidence": fact.confidence,
        "extraction_method": fact.extraction_method,
        "provenance": fact.provenance.value,
        "created_at": fact.created_at,
        "relation": relation,
        "related_fact_ids": related,
        "evidence": {"document_id": str(fact.document_id), "page": fact.source_page, "text": fact.source_text},
    }


def _event_item(event: HealthEvent) -> dict[str, Any]:
    return {
        "item_type": "HEALTH_EVENT",
        "id": str(event.id),
        "date": event.event_date,
        "date_precision": event.date_precision.value,
        "title": event.title,
        "description": event.description,
        "provenance": event.provenance.value,
        "source_type": event.source_type.value,
        "source_id": str(event.source_id) if event.source_id else None,
        "evidence": {"source_type": event.source_type.value, "source_id": str(event.source_id) if event.source_id else None},
    }


def _medication_item(medication: Medication) -> dict[str, Any]:
    return {
        "item_type": "MEDICATION",
        "id": str(medication.id),
        "date": medication.start_date,
        "date_precision": DatePrecision.EXACT.value if medication.start_date else DatePrecision.UNKNOWN.value,
        "title": medication.medicine_name,
        "details": {"dose": medication.dose, "frequency": medication.frequency, "route": medication.route, "status": medication.status.value},
        "provenance": medication.provenance.value,
        "source_id": str(medication.source_id) if medication.source_id else None,
        "evidence": {"source_id": str(medication.source_id) if medication.source_id else None},
    }


def _medication_event_item(event: MedicationEvent, medication: Medication) -> dict[str, Any]:
    return {
        "item_type": "MEDICATION_EVENT",
        "id": str(event.id),
        "date": event.event_date,
        "date_precision": DatePrecision.EXACT.value if event.event_date else DatePrecision.UNKNOWN.value,
        "title": f"{medication.medicine_name}: {event.event_type.value}",
        "details": {"dose": event.dose, "frequency": event.frequency, "notes": event.notes},
        "provenance": event.provenance.value,
        "source_id": str(medication.source_id) if medication.source_id else None,
        "evidence": {"source_id": str(medication.source_id) if medication.source_id else None},
    }


def _investigation_item(investigation: Investigation) -> dict[str, Any]:
    return {
        "item_type": "INVESTIGATION",
        "id": str(investigation.id),
        "date": investigation.performed_date or investigation.ordered_date,
        "date_precision": DatePrecision.EXACT.value if (investigation.performed_date or investigation.ordered_date) else DatePrecision.UNKNOWN.value,
        "title": investigation.name,
        "details": {"status": investigation.status.value, "ordered_date": investigation.ordered_date, "performed_date": investigation.performed_date, "notes": investigation.notes},
        "provenance": investigation.provenance.value,
        "evidence": {"source_id": None},
    }


def _investigation_result_item(result: InvestigationResult, investigation: Investigation) -> dict[str, Any]:
    return {
        "item_type": "INVESTIGATION_RESULT",
        "id": str(result.id),
        "date": result.result_date,
        "date_precision": DatePrecision.EXACT.value if result.result_date else DatePrecision.UNKNOWN.value,
        "title": investigation.name,
        "details": {"result_summary": result.result_summary, "notes": result.notes},
        "provenance": investigation.provenance.value,
        "source_id": str(result.source_id) if result.source_id else None,
        "evidence": {"source_id": str(result.source_id) if result.source_id else None},
    }


def _live_facts(db: Session, patient_id: UUID) -> list[HealthFact]:
    """Facts whose source document is still live. Facts of deleted / deletion-pending
    documents must never be surfaced to the patient or to shared viewers."""
    from db.models import Document, DocumentStatus
    return list(db.scalars(
        select(HealthFact).join(Document, Document.id == HealthFact.document_id)
        .where(HealthFact.patient_id == patient_id, Document.status.not_in([DocumentStatus.DELETED, DocumentStatus.DELETION_PENDING]))
    ).all())


def build_longitudinal_timeline(db: Session, patient_id: UUID, *, allowed_scopes: set[str] | None = None) -> list[dict[str, Any]]:
    allowed_scopes = {"DOCUMENTS","TIMELINE","MEDICATIONS","INVESTIGATIONS"} if allowed_scopes is None else set(allowed_scopes)
    facts = _live_facts(db, patient_id)
    items: list[dict[str, Any]] = ([_fact_item(f, facts) | {"item_type": "HEALTH_FACT", "date": f.temporal_date, "date_precision": f.temporal_precision.value, "title": f.fact_type} for f in facts] if "DOCUMENTS" in allowed_scopes else [])
    events = list(db.scalars(select(HealthEvent).where(HealthEvent.patient_id == patient_id)).all())
    if "TIMELINE" in allowed_scopes:
        items.extend(_event_item(e) for e in events)
    meds = list(db.scalars(select(Medication).where(Medication.patient_id == patient_id)).all())
    if "MEDICATIONS" in allowed_scopes:
        for med in meds:
            items.append(_medication_item(med))
            items.extend(_medication_event_item(ev, med) for ev in med.events)
    investigations = list(db.scalars(select(Investigation).where(Investigation.patient_id == patient_id)).all())
    if "INVESTIGATIONS" in allowed_scopes:
        for inv in investigations:
            items.append(_investigation_item(inv))
            items.extend(_investigation_result_item(result, inv) for result in inv.results)

    # Known dates sort chronologically; unknown dates are always last. Stable IDs make ties deterministic.
    return sorted(items, key=lambda x: (
        x["date"] is None,
        x["date"] or date.max,
        x["item_type"],
        x["id"],
    ))


def build_health_snapshot(db: Session, patient_id: UUID, *, allowed_scopes: set[str] | None = None) -> dict[str, Any]:
    allowed_scopes = {"DOCUMENTS","TIMELINE","MEDICATIONS","INVESTIGATIONS"} if allowed_scopes is None else set(allowed_scopes)
    facts = sorted(_live_facts(db, patient_id), key=lambda f: (f.temporal_date is None, -(f.temporal_date.toordinal() if f.temporal_date else 0), -f.created_at.timestamp(), str(f.id)))
    medications = list(db.scalars(select(Medication).where(Medication.patient_id == patient_id).order_by(Medication.start_date.desc().nullslast(), Medication.created_at.desc(), Medication.id.desc())).all())
    investigations = list(db.scalars(select(Investigation).where(Investigation.patient_id == patient_id).order_by(Investigation.performed_date.desc().nullslast(), Investigation.created_at.desc(), Investigation.id.desc())).all())
    events = list(db.scalars(select(HealthEvent).where(HealthEvent.patient_id == patient_id).order_by(HealthEvent.event_date.desc().nullslast(), HealthEvent.created_at.desc(), HealthEvent.id.desc())).all())

    def supported(f: HealthFact, prefixes: tuple[str, ...]) -> bool:
        return f.fact_type.upper().startswith(prefixes)

    return {
        "patient_id": str(patient_id),
        "medications": [_medication_item(m) for m in medications if m.status.value == "ACTIVE" or m.end_date is not None] if "MEDICATIONS" in allowed_scopes else [],
        "investigations": ([_investigation_item(i) for i in investigations] + [_investigation_result_item(r, i) for i in investigations for r in i.results]) if "INVESTIGATIONS" in allowed_scopes else [],
        "diagnoses_conditions": [_fact_item(f, facts) for f in facts if supported(f, ("DIAGNOSIS", "CONDITION"))] if "DOCUMENTS" in allowed_scopes else [],
        "vaccinations": [_fact_item(f, facts) for f in facts if supported(f, ("VACCIN",)) or f.fact_type.upper() == "VACCINATION"] if "DOCUMENTS" in allowed_scopes else [],
        "major_events": [_event_item(e) for e in events] if "TIMELINE" in allowed_scopes else [],
        "document_facts": [_fact_item(f, facts) for f in facts if not supported(f, ("DIAGNOSIS", "CONDITION", "VACCIN"))] if "DOCUMENTS" in allowed_scopes else [],
        "evidence_policy": "All entries are retained as recorded with their provenance/evidence; no clinical inference or recommendation is performed.",
    }
