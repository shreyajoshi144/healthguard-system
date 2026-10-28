"""Consumer health overview API (dashboard, summaries, doctor questions, tracker,
unified timeline, health graph, family overview). Read-only, scope-aware and
record-derived; see domain/consumer.py for the safety contract."""
from __future__ import annotations
from datetime import datetime, timezone
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from backend.auth import get_current_user
from db.session import get_db
from db.models import PatientProfile, PatientAccess, AccessGrantStatus, TemporaryShare, User
from domain.audit import audit
from domain.authorization import active_grant
from domain import consumer as C

router = APIRouter(prefix="/api")


def _scopes_and_patient(db: Session, user: User, patient_id: UUID) -> tuple[PatientProfile, set[str], bool]:
    patient = db.get(PatientProfile, patient_id)
    if not patient:
        raise HTTPException(404, "Patient not found.")
    if patient.owner_user_id == user.id:
        return patient, set(C.ALL_SCOPES), True
    grant = active_grant(db, patient_id, user.id)
    if not grant:
        raise HTTPException(403, "You do not have access to this patient.")
    scopes = set(grant.scopes or []) & C.ALL_SCOPES
    if not scopes:
        raise HTTPException(403, "No scope is granted for this patient.")
    return patient, scopes, False


def _ctx(db, user, patient_id):
    patient, scopes, is_owner = _scopes_and_patient(db, user, patient_id)
    return C.Ctx(db, patient, scopes), is_owner


@router.get("/patients/{patient_id}/dashboard")
def dashboard(patient_id: UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    ctx, is_owner = _ctx(db, user, patient_id)
    family = None
    if is_owner:
        now = datetime.now(timezone.utc)
        grants = [g for g in db.scalars(select(PatientAccess).where(PatientAccess.patient_id == patient_id, PatientAccess.status == AccessGrantStatus.ACTIVE, PatientAccess.revoked_at.is_(None))).all() if g.expires_at is None or g.expires_at.replace(tzinfo=g.expires_at.tzinfo or timezone.utc) > now]
        shares = db.scalar(select(func.count()).select_from(TemporaryShare).where(TemporaryShare.patient_id == patient_id, TemporaryShare.revoked_at.is_(None), TemporaryShare.expires_at > now)) or 0
        family = {"active_relationships": len(grants), "active_temporary_shares": int(shares), "relationships": [{"role": g.role.value, "scopes": list(g.scopes or []), "expires_at": g.expires_at} for g in grants[:5]]}
    return C.build_dashboard(ctx, family=family)


@router.get("/patients/{patient_id}/summary")
def summary(patient_id: UUID, mode: str = Query("short", pattern="^(short|detailed)$"), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    ctx, _ = _ctx(db, user, patient_id)
    result = C.build_summary(ctx, mode)
    audit(db, user, "HEALTH_SUMMARY_GENERATED", "patient", patient_id, context={"mode": mode})
    db.commit()
    return result


@router.get("/patients/{patient_id}/doctor-questions")
def doctor_questions(patient_id: UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    ctx, _ = _ctx(db, user, patient_id)
    result = C.build_doctor_questions(ctx)
    audit(db, user, "DOCTOR_QUESTIONS_GENERATED", "patient", patient_id, context={"question_count": len(result["suggested_questions"])})
    db.commit()
    return result


@router.get("/patients/{patient_id}/medication-tracker")
def medication_tracker(patient_id: UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    ctx, _ = _ctx(db, user, patient_id)
    if "MEDICATIONS" not in ctx.scopes:
        raise HTTPException(403, "The medication scope is not granted for this patient.")
    return C.build_medication_tracker(ctx)


@router.get("/patients/{patient_id}/unified-timeline")
def unified_timeline(patient_id: UUID, types: str | None = Query(default=None, max_length=200), limit: int = Query(200, ge=1, le=500), offset: int = Query(0, ge=0, le=100000), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    ctx, _ = _ctx(db, user, patient_id)
    wanted = {t.strip().upper() for t in types.split(",") if t.strip()} if types else None
    return C.build_unified_timeline(db, ctx, types=wanted, limit=limit, offset=offset)


@router.get("/patients/{patient_id}/health-graph")
def health_graph(patient_id: UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    ctx, _ = _ctx(db, user, patient_id)
    result = C.build_health_graph(ctx)
    audit(db, user, "HEALTH_GRAPH_VIEWED", "patient", patient_id, context={"node_count": result["node_count"]})
    db.commit()
    return result


@router.get("/family/overview")
def family_overview(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    now = datetime.now(timezone.utc)
    profiles = []
    for p in db.scalars(select(PatientProfile).where(PatientProfile.owner_user_id == user.id).order_by(PatientProfile.created_at)).all():
        grants = [g for g in db.scalars(select(PatientAccess).where(PatientAccess.patient_id == p.id, PatientAccess.status == AccessGrantStatus.ACTIVE, PatientAccess.revoked_at.is_(None))).all() if active_grant(db, p.id, g.user_id)]
        shares = db.scalar(select(func.count()).select_from(TemporaryShare).where(TemporaryShare.patient_id == p.id, TemporaryShare.revoked_at.is_(None), TemporaryShare.expires_at > now)) or 0
        profiles.append({"patient_id": str(p.id), "name": f"{p.first_name} {p.last_name}", "relationship": "OWNER", "is_owner": True, "scopes": sorted(C.ALL_SCOPES), "write_scopes": sorted(C.ALL_SCOPES), "expires_at": None,
                         "active_relationships": len(grants), "active_temporary_shares": int(shares)})
    for g in db.scalars(select(PatientAccess).where(PatientAccess.user_id == user.id, PatientAccess.status == AccessGrantStatus.ACTIVE, PatientAccess.revoked_at.is_(None))).all():
        if not active_grant(db, g.patient_id, user.id):
            continue
        p = db.get(PatientProfile, g.patient_id)
        if not p or p.owner_user_id == user.id:
            continue
        scopes = set(g.scopes or [])
        profiles.append({"patient_id": str(p.id), "name": f"{p.first_name} {p.last_name}" if "PROFILE" in scopes else "Shared profile", "relationship": g.role.value, "is_owner": False,
                         "scopes": sorted(scopes), "write_scopes": sorted(g.write_scopes or []), "expires_at": g.expires_at, "active_relationships": None, "active_temporary_shares": None})
    return {"profiles": profiles}
