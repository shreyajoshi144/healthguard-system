from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai.answer_provider import GROUNDED_SYSTEM_INSTRUCTIONS, get_health_record_answer_provider
from db.models import Handoff, HandoffStatus, PatientProfile
from domain.grounding import validate_grounded_answer, SAFE_INSUFFICIENT_ANSWER
from domain.retrieval import retrieve_patient_hybrid_evidence

HANDOFF_SYSTEM_INSTRUCTIONS = GROUNDED_SYSTEM_INSTRUCTIONS + """

You are preparing a patient-owned doctor/second-opinion handoff draft.
Organize only documented patient context from the supplied evidence. Do not diagnose,
prescribe, recommend treatment or medication changes, infer adherence, invent dates,
or decide which clinician's treatment was correct. Surface conflicts and uncertainty.
If a requested section has no supporting information, say it is unavailable rather than
filling the gap. Keep provenance distinctions visible when relevant.
"""


def build_handoff_evidence(db: Session, patient_id: UUID, *, limit: int = 50, allowed_scopes: set[str] | None = None) -> list[dict[str, Any]]:
    query = "medication history investigations results consultation hospitalization surgery discharge follow-up documents health history"
    rows = retrieve_patient_hybrid_evidence(db, patient_id, query, limit=limit, allowed_scopes=allowed_scopes)
    for index, item in enumerate(rows, 1):
        item.setdefault("evidence_id", f"evidence_{index}")
    return rows


def _snapshot_value(value: Any) -> Any:
    if isinstance(value, (UUID, datetime, date)):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _snapshot_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_snapshot_value(v) for v in value]
    return value


def evidence_snapshot(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_snapshot_value(dict(item)) for item in evidence]


def _scope_for_evidence_type(source_type: str) -> str | None:
    if source_type in {"MEDICATION", "MEDICATION_EVENT"}:
        return "MEDICATIONS"
    if source_type in {"INVESTIGATION", "INVESTIGATION_RESULT"}:
        return "INVESTIGATIONS"
    if source_type in {"HEALTH_EVENT"}:
        return "TIMELINE"
    if source_type in {"HEALTH_FACT", "DOCUMENT_TEXT_BLOCK"}:
        return "DOCUMENTS"
    return None


def authorized_snapshot_for_handoff(
    db: Session, artifact: Handoff, *, allowed_scopes: set[str] | None = None
) -> list[dict[str, Any]]:
    metadata = artifact.generation_metadata or {}
    snapshot = metadata.get("evidence_snapshot")
    if not isinstance(snapshot, list):
        # Backward-compatible drafts created before the snapshot field existed:
        # use their original source IDs only, never newly retrieved evidence.
        refs = {str(ref.get("source_id")) for ref in (artifact.evidence_refs or []) if ref.get("source_id")}
        current = build_handoff_evidence(db, artifact.patient_id, allowed_scopes=allowed_scopes)
        return [item for item in current if str(item.get("source_id")) in refs]

    allowed = set(allowed_scopes or set())
    if allowed_scopes is not None:
        for item in snapshot:
            if str(item.get("patient_id")) != str(artifact.patient_id):
                raise ValueError("Handoff evidence snapshot is not patient-scoped.")
            required = _scope_for_evidence_type(str(item.get("source_type") or ""))
            if required and required not in allowed:
                raise ValueError("Handoff evidence is no longer within the authorized access scope.")

    # Confirm the reviewed sources still exist for this patient. The current rows
    # are used only for existence/ownership authorization; the snapshot remains
    # the evidence used for grounding, so new/unrelated evidence cannot replace it.
    refs = {str(item.get("source_id")) for item in snapshot if item.get("source_id")}
    current = build_handoff_evidence(db, artifact.patient_id, allowed_scopes=allowed_scopes)
    current_ids = {str(item.get("source_id")) for item in current}
    if not refs.issubset(current_ids):
        raise ValueError("Reviewed handoff evidence is no longer available for finalization.")
    return snapshot


def generate_handoff(db: Session, patient: PatientProfile, *, title: str, handoff_type: str, allowed_scopes: set[str] | None = None) -> Handoff:
    evidence = build_handoff_evidence(db, patient.id, allowed_scopes=allowed_scopes)
    if not evidence:
        context_bits=[]
        if allowed_scopes is None or "PROFILE" in allowed_scopes:
            context_bits.append(f"Patient: {patient.first_name} {patient.last_name}")
        if (allowed_scopes is None or "PROFILE" in allowed_scopes) and patient.date_of_birth: context_bits.append(f"Date of birth: {patient.date_of_birth.isoformat()}")
        if (allowed_scopes is None or "PROFILE" in allowed_scopes) and patient.gender: context_bits.append(f"Gender: {patient.gender}")
        if (allowed_scopes is None or "PROFILE" in allowed_scopes) and patient.blood_group: context_bits.append(f"Blood group: {patient.blood_group}")
        content="\n".join(context_bits+["", "Patient health history is unavailable from the current record."])
        artifact = Handoff(patient_id=patient.id, title=title, handoff_type=handoff_type,
                           status=HandoffStatus.DRAFT, content=content, evidence_refs=[],
                           generation_metadata={"grounded": False, "reason": "no evidence"})
        db.add(artifact); db.flush(); return artifact

    provider = get_health_record_answer_provider()
    question = (
        "Prepare a concise doctor/second-opinion handoff draft from the supplied records. "
        "Include patient context only from supplied context, relevant health history, medication history, "
        "investigations/results, documented changes, pending documented follow-ups when present, and conflicts/uncertainty."
    )
    result = provider.answer(question=question, evidence=evidence, system_instructions=HANDOFF_SYSTEM_INSTRUCTIONS)
    validation = validate_grounded_answer(result, evidence, expected_patient_id=str(patient.id))
    if not validation.valid:
        raise ValueError(validation.reason or "Handoff generation could not be grounded.")

    context_bits=[]
    if allowed_scopes is None or "PROFILE" in allowed_scopes:
        context_bits.append(f"Patient: {patient.first_name} {patient.last_name}")
    if (allowed_scopes is None or "PROFILE" in allowed_scopes) and patient.date_of_birth: context_bits.append(f"Date of birth: {patient.date_of_birth.isoformat()}")
    if (allowed_scopes is None or "PROFILE" in allowed_scopes) and patient.gender: context_bits.append(f"Gender: {patient.gender}")
    if (allowed_scopes is None or "PROFILE" in allowed_scopes) and patient.blood_group: context_bits.append(f"Blood group: {patient.blood_group}")
    final_content="\n".join(context_bits+["",validation.answer])
    refs = [{"evidence_id": item["evidence_id"], "source_type": item.get("source_type"),
             "source_id": str(item.get("source_id")), "document_id": str(item["document_id"]) if item.get("document_id") else None,
             "fact_id": str(item["fact_id"]) if item.get("fact_id") else None,
             "page": item.get("page"), "provenance": item.get("provenance")} for item in evidence]
    artifact = Handoff(patient_id=patient.id, title=title, handoff_type=handoff_type,
                       status=HandoffStatus.DRAFT, content=final_content, evidence_refs=refs,
                       generation_metadata={"grounded": validation.grounded, "citations": validation.citations,
                                            "uncertainty": validation.uncertainty, "retrieval": "hybrid",
                                            "evidence_snapshot": evidence_snapshot(evidence)})
    db.add(artifact); db.flush(); return artifact


def evidence_for_handoff(db: Session, artifact: Handoff, *, allowed_scopes: set[str] | None = None) -> list[dict[str, Any]]:
    return build_handoff_evidence(db, artifact.patient_id, allowed_scopes=allowed_scopes)
