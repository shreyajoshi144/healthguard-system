"""Privacy controls: explicit consents and patient data export."""
from __future__ import annotations

import json
import tempfile
import zipfile
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.auth import get_current_user
from db.models import Document, DocumentStatus, PatientProfile, ProcessingJob, User
from db.session import get_db
from domain.audit import audit
from domain.consent import list_consents, set_consent
from domain.export import build_patient_export, original_documents
from domain.rate_limit import rate_limit
from storage.service import sanitize_filename, storage

router = APIRouter(prefix="/api")
NO_STORE = {"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache", "X-Content-Type-Options": "nosniff"}


class ConsentDecision(BaseModel):
    granted: bool


@router.get("/privacy/consents")
def get_consents(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return JSONResponse(jsonable(list_consents(db, user)), headers=NO_STORE)


def jsonable(items):
    return [{k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in i.items()} for i in items]


def _requeue_consent_blocked(db: Session, user: User) -> int:
    """After consent is granted, documents that were waiting for it are processed instead of staying failed."""
    from jobs.queue import QueueUnavailable, enqueue_document
    from jobs.worker import CONSENT_BLOCKED
    jobs = db.scalars(select(ProcessingJob).join(Document, Document.id == ProcessingJob.document_id).join(PatientProfile, PatientProfile.id == Document.patient_id)
                      .where(PatientProfile.owner_user_id == user.id, ProcessingJob.last_error == CONSENT_BLOCKED, Document.status == DocumentStatus.FAILED)).all()
    for job in jobs:
        job.status = DocumentStatus.QUEUED; job.attempts = 0; job.last_error = None; job.next_attempt_at = None; job.lease_expires_at = None
        db.get(Document, job.document_id).status = DocumentStatus.QUEUED
    db.commit()
    for job in jobs:
        try: enqueue_document(str(job.id))
        except QueueUnavailable: pass            # durable QUEUED state is picked up by the worker's recovery loop
    return len(jobs)


@router.put("/privacy/consents/{purpose}")
def put_consent(purpose: str, decision: ConsentDecision, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    row = set_consent(db, user, purpose, decision.granted)
    audit(db, user, "CONSENT_GRANTED" if decision.granted else "CONSENT_WITHDRAWN", "consent", row.id, context={"purpose": purpose, "policy_version": row.policy_version})
    db.commit()
    requeued = _requeue_consent_blocked(db, user) if decision.granted and purpose == "AI_PROCESSING" else 0
    return JSONResponse({**next(c for c in jsonable(list_consents(db, user)) if c["purpose"] == purpose), "requeued_documents": requeued}, headers=NO_STORE)


@router.get("/patients/{patient_id}/export")
def export_patient(patient_id: UUID, format: str = Query("json", pattern="^(json|zip)$"), db: Session = Depends(get_db),
                   user: User = Depends(get_current_user), _rl=Depends(rate_limit("patient_export", 5))):
    patient = db.get(PatientProfile, patient_id)
    if not patient:
        raise HTTPException(404, "Patient not found.")
    if patient.owner_user_id != user.id:
        # Data portability is the record owner's right; people the record is shared with can view it but not export a copy.
        raise HTTPException(403, "Only the record owner can export this patient's data.")
    bundle = build_patient_export(db, patient, user)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    if format == "json":
        audit(db, user, "PATIENT_EXPORTED", "patient", patient_id, context={"format": "json", "counts": bundle["counts"]}); db.commit()
        return JSONResponse(bundle, headers={**NO_STORE, "Content-Disposition": f'attachment; filename="healthguard-export-{stamp}.json"'})
    spool = tempfile.SpooledTemporaryFile(max_size=32 * 1024 * 1024)
    missing: list[str] = []
    with zipfile.ZipFile(spool, "w", zipfile.ZIP_DEFLATED) as z:
        for d in original_documents(db, patient):
            try:
                z.writestr(f"documents/{d.id}-{sanitize_filename(d.filename)}", storage.get(d.storage_key))
            except Exception:
                missing.append(str(d.id))
        bundle["original_files_not_included"] = missing
        z.writestr("healthguard-export.json", json.dumps(bundle, indent=1, ensure_ascii=False))
        z.writestr("README.txt", "healthguard-export.json holds the structured record. documents/ holds your original uploaded files. "
                                 "original_files_not_included lists any file that could not be read from storage.\n")
    audit(db, user, "PATIENT_EXPORTED", "patient", patient_id, context={"format": "zip", "counts": bundle["counts"], "files_missing": len(missing)}); db.commit()
    spool.seek(0)

    def stream():
        try:
            while chunk := spool.read(1024 * 1024):
                yield chunk
        finally:
            spool.close()
    return StreamingResponse(stream(), media_type="application/zip", headers={**NO_STORE, "Content-Disposition": f'attachment; filename="healthguard-export-{stamp}.zip"'})
