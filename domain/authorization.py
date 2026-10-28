from __future__ import annotations
from uuid import UUID
from datetime import datetime, timezone
from sqlalchemy import select, and_
from sqlalchemy.orm import Session
from fastapi import HTTPException
from db.models import PatientProfile, PatientAccess, User, AccessRole, AccessGrantStatus, AccessScope, ProcessingJob, Document, Provenance

ALL_SCOPES={s.value for s in AccessScope}
WRITE_ROLES={AccessRole.FAMILY, AccessRole.CAREGIVER}
GRANTABLE_ROLES={AccessRole.FAMILY, AccessRole.CAREGIVER}

# Single source of truth for resource authorization. API handlers must pass the
# corresponding scope to require_scope/resource_patient; tests audit this map
# against the protected resource families. OWNER bypasses grant checks, while
# non-owners require the mapped read scope and, for mutations, its write scope.
AUTHORIZATION_MATRIX = {
    "patient_profile": AccessScope.PROFILE,
    "health_event": AccessScope.TIMELINE,
    "medication": AccessScope.MEDICATIONS,
    "medication_event": AccessScope.MEDICATIONS,
    "investigation": AccessScope.INVESTIGATIONS,
    "investigation_result": AccessScope.INVESTIGATIONS,
    "document": AccessScope.DOCUMENTS,
    "processing_job": AccessScope.DOCUMENTS,
    "health_fact": AccessScope.DOCUMENTS,
    "handoff": AccessScope.HANDOFFS,
    "health_action": AccessScope.HEALTH_ACTIONS,
    "measurement": AccessScope.MEASUREMENTS,
    "retrieval": "ANY_RELEVANT_RECORD_SCOPE",
    "ask": "ANY_RELEVANT_RECORD_SCOPE",
    "temporary_share_management": AccessScope.PROFILE,
}

def _utc(value):
    if value is None: return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value

def _scope_name(scope):
    return scope.value if isinstance(scope, AccessScope) else str(scope).upper()

def _grant_active(db:Session, patient_id:UUID, user_id:UUID):
    grant=db.scalar(select(PatientAccess).where(and_(PatientAccess.patient_id==patient_id,PatientAccess.user_id==user_id,PatientAccess.revoked_at.is_(None),PatientAccess.status==AccessGrantStatus.ACTIVE)))
    if grant and grant.expires_at is not None and _utc(grant.expires_at) <= datetime.now(timezone.utc):
        grant.status=AccessGrantStatus.EXPIRED
        db.commit()
        return None
    return grant

def patient_for_user(db:Session,user:User,patient_id:UUID,*,write:bool=False,scope:AccessScope|str|None=None)->PatientProfile:
    if not user: raise HTTPException(status_code=401,detail="Not authenticated.")
    patient=db.get(PatientProfile,patient_id)
    if not patient: raise HTTPException(status_code=404,detail="Patient not found.")
    if patient.owner_user_id==user.id: return patient
    grant=_grant_active(db,patient_id,user.id)
    if not grant: raise HTTPException(status_code=403,detail="You do not have access to this patient.")
    wanted=_scope_name(scope) if scope is not None else None
    read_scopes=set(grant.scopes or [])
    write_scopes=set(grant.write_scopes or [])
    if wanted is not None and wanted not in read_scopes:
        raise HTTPException(status_code=403,detail="The requested access scope is not permitted.")
    if write:
        if wanted is None:
            raise HTTPException(status_code=403,detail="A write scope is required.")
        if wanted not in write_scopes:
            raise HTTPException(status_code=403,detail="Write access is not permitted for this scope.")
    return patient

def require_scope(db:Session,user:User,patient_id:UUID,scope:AccessScope,*,write:bool=False)->PatientProfile:
    return patient_for_user(db,user,patient_id,write=write,scope=scope)

def active_grant(db:Session,patient_id:UUID,user_id:UUID):
    return _grant_active(db,patient_id,user_id)

def resource_patient(db:Session,user:User,model,resource_id:UUID,*,scope:AccessScope|str|None=None,write:bool=False):
    resource=db.get(model,resource_id)
    if not resource: raise HTTPException(status_code=404,detail="Resource not found.")
    patient_id=getattr(resource,"patient_id",None)
    if patient_id is None: raise HTTPException(status_code=500,detail="Resource is missing patient ownership metadata.")
    if scope is None:
        if not user or patient_id is None:
            raise HTTPException(status_code=403,detail="A resource access scope is required.")
        patient=db.get(PatientProfile,patient_id)
        if not patient or patient.owner_user_id!=user.id:
            raise HTTPException(status_code=403,detail="A resource access scope is required.")
        if write:
            raise HTTPException(status_code=403,detail="A resource write scope is required.")
    patient=patient_for_user(db,user,patient_id,write=write,scope=scope)
    return resource,patient

def processing_job_for_user(db:Session,user:User,job_id:UUID,*,write:bool=False):
    job=db.get(ProcessingJob,job_id)
    if not job: raise HTTPException(status_code=404,detail="Processing job not found.")
    document=db.get(Document,job.document_id)
    if not document: raise HTTPException(status_code=404,detail="Processing job document not found.")
    patient=patient_for_user(db,user,document.patient_id,write=write,scope=AccessScope.DOCUMENTS)
    return job,document,patient

def provenance_for_actor(db:Session,user:User,patient_id:UUID)->Provenance:
    patient=patient_for_user(db,user,patient_id)
    return Provenance.USER_REPORTED if patient.owner_user_id==user.id else Provenance.FAMILY_REPORTED
