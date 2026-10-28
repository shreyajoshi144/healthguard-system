from __future__ import annotations
import hashlib, secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID
from sqlalchemy import select, and_
from sqlalchemy.orm import Session
from fastapi import HTTPException
from db.models import PatientAccess, PatientProfile, User, AccessRole, AccessGrantStatus, AccessScope, TemporaryShare, Handoff, HandoffStatus, Document, DocumentStatus, Medication, Investigation, HealthEvent, HealthAction

ALL_SCOPES=[x.value for x in AccessScope]
GRANTEE_ROLES={AccessRole.FAMILY,AccessRole.CAREGIVER}
SHARE_ROLES={AccessRole.DOCTOR}

def now(): return datetime.now(timezone.utc)

def _aware(value):
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value

def normalize_scopes(scopes:list[str]|None)->list[str]:
    values=[str(x).upper() for x in (scopes or [])]
    if not values: raise HTTPException(422,"At least one access scope is required.")
    if any(x not in ALL_SCOPES for x in values): raise HTTPException(422,"Unsupported access scope.")
    return sorted(set(values))

def grant_relationship(db:Session,*,patient:PatientProfile,actor:User,target:User,role:AccessRole,scopes:list[str],write_scopes:list[str]|None,expires_at:datetime|None):
    if patient.owner_user_id != actor.id: raise HTTPException(403,"Only the patient owner can grant access.")
    if target.id == actor.id: raise HTTPException(409,"A user cannot grant access to themselves.")
    if role not in GRANTEE_ROLES: raise HTTPException(422,"Only FAMILY or CAREGIVER access can be granted.")
    expires_at=_aware(expires_at)
    if expires_at is not None and expires_at <= now(): raise HTTPException(422,"Access expiration must be in the future.")
    scopes=normalize_scopes(scopes)
    write_scopes=normalize_scopes(write_scopes) if write_scopes else []
    if not set(write_scopes).issubset(set(scopes)):
        raise HTTPException(422,"Every write scope must also be granted as a read scope.")
    grant=db.scalar(select(PatientAccess).where(PatientAccess.patient_id==patient.id,PatientAccess.user_id==target.id))
    if grant and grant.revoked_at is None and grant.status==AccessGrantStatus.ACTIVE:
        expiry = grant.expires_at.replace(tzinfo=timezone.utc) if grant.expires_at is not None and grant.expires_at.tzinfo is None else grant.expires_at
        if expiry is None or expiry > now(): raise HTTPException(409,"An active access grant already exists.")
        grant.status=AccessGrantStatus.EXPIRED
    if grant:
        grant.role=role; grant.status=AccessGrantStatus.ACTIVE; grant.scopes=scopes; grant.write_scopes=write_scopes; grant.expires_at=expires_at; grant.revoked_at=None; grant.granted_by=actor.id
    else:
        grant=PatientAccess(patient_id=patient.id,user_id=target.id,role=role,granted_by=actor.id,status=AccessGrantStatus.ACTIVE,scopes=scopes,write_scopes=write_scopes,expires_at=expires_at)
        db.add(grant)
    db.flush(); return grant

def revoke_relationship(db:Session,*,patient_id:UUID,target_user_id:UUID,actor:User):
    patient=db.get(PatientProfile,patient_id)
    if not patient or patient.owner_user_id!=actor.id: raise HTTPException(403,"Only the patient owner can revoke access.")
    grant=db.scalar(select(PatientAccess).where(PatientAccess.patient_id==patient_id,PatientAccess.user_id==target_user_id,PatientAccess.revoked_at.is_(None)))
    if not grant: raise HTTPException(404,"Access grant not found.")
    grant.revoked_at=now(); grant.status=AccessGrantStatus.REVOKED; db.flush(); return grant

def validate_resource(db:Session,patient_id:UUID,scope:str,resource_id:UUID):
    model={
        "DOCUMENTS":Document,"MEDICATIONS":Medication,"INVESTIGATIONS":Investigation,"TIMELINE":HealthEvent,"HANDOFFS":Handoff,"HEALTH_ACTIONS":HealthAction
    }.get(scope)
    if model is None: raise HTTPException(422,"This scope requires explicit resource selection.")
    obj=db.get(model,resource_id)
    if not obj or getattr(obj,"patient_id",None)!=patient_id: raise HTTPException(404,"Shared resource not found.")
    if model is Handoff and obj.status!=HandoffStatus.FINAL: raise HTTPException(409,"Only finalized handoffs may be shared.")
    if model is Document and obj.status!=DocumentStatus.COMPLETED: raise HTTPException(409,"Only completed documents may be shared.")
    return obj

def create_share(db:Session,*,patient:PatientProfile,actor:User,scopes:list[str],resources:dict,expires_at:datetime):
    if patient.owner_user_id!=actor.id: raise HTTPException(403,"Only the patient owner can create a temporary share.")
    expires_at=_aware(expires_at)
    if expires_at<=now(): raise HTTPException(422,"Share expiration must be in the future.")
    scopes=normalize_scopes(scopes)
    resources={str(k).upper():[str(x) for x in (v or [])] for k,v in (resources or {}).items()}
    for scope,ids in resources.items():
        if scope not in scopes: raise HTTPException(422,"Every shared resource must be covered by its scope.")
        if scope == "PROFILE":
            raise HTTPException(422,"PROFILE does not support resource IDs.")
        if scope not in {"MEDICATIONS","INVESTIGATIONS","TIMELINE","DOCUMENTS","HANDOFFS","HEALTH_ACTIONS"}:
            raise HTTPException(422,"Unsupported shared resource scope.")
        for rid in ids: validate_resource(db,patient.id,scope,UUID(rid))
    raw=secrets.token_urlsafe(48)
    share=TemporaryShare(patient_id=patient.id,created_by=actor.id,token_hash=hashlib.sha256(raw.encode()).hexdigest(),role=AccessRole.DOCTOR,scopes=scopes,resources=resources,expires_at=expires_at,created_at=now(),updated_at=now())
    db.add(share); db.flush(); return share,raw

def resolve_share(db:Session,raw_token:str):
    if not raw_token or len(raw_token)<40: raise HTTPException(404,"Share not found or expired.")
    digest=hashlib.sha256(raw_token.encode()).hexdigest()
    share=db.scalar(select(TemporaryShare).where(TemporaryShare.token_hash==digest))
    expiry = share.expires_at.replace(tzinfo=timezone.utc) if share and share.expires_at is not None and share.expires_at.tzinfo is None else (share.expires_at if share else None)
    if not share or share.revoked_at is not None or expiry<=now(): raise HTTPException(404,"Share not found or expired.")
    return share
