from __future__ import annotations
from sqlalchemy import select
from sqlalchemy.orm import Session
from db.models import Medication, MedicationEvent, HealthFact, Document, Provenance
from domain.retrieval import _medication_candidate, _medication_event_candidate, _fact_candidate
from domain.grounding import validate_grounded_answer, GroundingValidation, SAFE_INSUFFICIENT_ANSWER
from domain.ai_safety import classify_medical_safety
from ai.answer_provider import AnswerClaim, AnswerResult

def safety_flags(question: str) -> list[str]:
    """Single medication-safety policy entry point.

    Medication context must use the same defense-in-depth policy as Ask My
    Records and grounding validation; it must not maintain a second regex
    policy that can drift from the canonical AI safety policy.
    """
    decision = classify_medical_safety(question)
    return list(decision.flags)

def medication_context(db:Session,patient_id,medication_id,question,*,allowed_scopes=None):
    med=db.scalar(select(Medication).where(Medication.id==medication_id,Medication.patient_id==patient_id))
    if not med: return None
    events=list(db.scalars(select(MedicationEvent).where(MedicationEvent.medication_id==med.id).order_by(MedicationEvent.event_date.asc().nullslast(),MedicationEvent.created_at.asc())).all())
    facts=list(db.scalars(select(HealthFact).where(HealthFact.patient_id==patient_id,HealthFact.document_id==Medication.source_id)).all()) if med.source_id and (allowed_scopes is None or "DOCUMENTS" in allowed_scopes) else []
    evidence=[]
    for item in [_medication_candidate(med),*[_medication_event_candidate(e,med) for e in events],*[_fact_candidate(f) for f in facts]]:
        if item: evidence.append(item)
    return med,evidence
