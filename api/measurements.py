"""Sprint 6 — health measurements (vitals) API.

Every route is server-authorized through the MEASUREMENTS scope; resource-level
routes resolve the owning patient before authorizing, so a measurement id from
another patient is never readable. Audit contexts carry the measurement *kind*
only, never values.
"""
from __future__ import annotations
from datetime import datetime, timezone
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from backend.auth import get_current_user
from db.session import get_db
from db.models import Measurement, User, AccessScope, EventSource
from domain.audit import audit
from domain.authorization import require_scope, resource_patient, provenance_for_actor
from domain.measurements import SPECS, MeasurementValidationError, validate_measurement, build_series, spec_catalog
from domain.rate_limit import rate_limit

router = APIRouter(prefix="/api")
MAX_PAGE_SIZE = 100

class MeasurementCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(min_length=1, max_length=32)
    value: float = Field(allow_inf_nan=False)
    value_secondary: float | None = Field(default=None, allow_inf_nan=False)
    unit: str | None = Field(default=None, max_length=16)
    measured_at: datetime
    context: str | None = Field(default=None, max_length=32)
    note: str | None = Field(default=None, max_length=500)

class MeasurementUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: float | None = Field(default=None, allow_inf_nan=False)
    value_secondary: float | None = Field(default=None, allow_inf_nan=False)
    unit: str | None = Field(default=None, max_length=16)
    measured_at: datetime | None = None
    context: str | None = Field(default=None, max_length=32)
    note: str | None = Field(default=None, max_length=500)

def _out(m: Measurement) -> dict:
    return {
        "id": str(m.id), "patient_id": str(m.patient_id), "kind": m.kind, "value": m.value, "value_secondary": m.value_secondary,
        "unit": m.unit, "measured_at": m.measured_at, "context": m.context, "note": m.note,
        "provenance": m.provenance.value, "source_type": m.source_type.value,
        "source_document_id": str(m.source_document_id) if m.source_document_id else None,
        "source_fact_id": str(m.source_fact_id) if m.source_fact_id else None,
        "created_at": m.created_at, "updated_at": m.updated_at,
    }

def _validated(kind, value, value_secondary, unit, context, measured_at):
    try:
        return validate_measurement(kind, value, value_secondary, unit, context, measured_at)
    except MeasurementValidationError as exc:
        raise HTTPException(422, str(exc)) from exc

@router.get("/measurement-types")
def measurement_types(user: User = Depends(get_current_user)):
    return {"types": spec_catalog()}

@router.post("/patients/{patient_id}/measurements", status_code=201)
def create_measurement(patient_id: UUID, payload: MeasurementCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user), _rl=Depends(rate_limit("measurement_write", 120))):
    require_scope(db, user, patient_id, AccessScope.MEASUREMENTS, write=True)
    kind = payload.kind.upper()
    unit, context = _validated(kind, payload.value, payload.value_secondary, payload.unit, payload.context, payload.measured_at)
    m = Measurement(patient_id=patient_id, kind=kind, value=payload.value, value_secondary=payload.value_secondary, unit=unit,
                    measured_at=payload.measured_at if payload.measured_at.tzinfo else payload.measured_at.replace(tzinfo=timezone.utc),
                    context=context, note=payload.note, provenance=provenance_for_actor(db, user, patient_id), source_type=EventSource.MANUAL, created_by=user.id)
    db.add(m); db.flush()
    audit(db, user, "MEASUREMENT_CREATED", "measurement", m.id, context={"kind": kind})
    db.commit(); db.refresh(m)
    return _out(m)

@router.get("/patients/{patient_id}/measurements")
def list_measurements(patient_id: UUID, kind: str | None = Query(default=None, max_length=32), start: datetime | None = None, end: datetime | None = None,
                      page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=MAX_PAGE_SIZE), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    require_scope(db, user, patient_id, AccessScope.MEASUREMENTS)
    if start and end and end < start:
        raise HTTPException(422, "end cannot precede start.")
    stmt = select(Measurement).where(Measurement.patient_id == patient_id)
    if kind:
        if kind.upper() not in SPECS: raise HTTPException(422, "Unsupported measurement type.")
        stmt = stmt.where(Measurement.kind == kind.upper())
    if start: stmt = stmt.where(Measurement.measured_at >= start)
    if end: stmt = stmt.where(Measurement.measured_at <= end)
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    stmt = stmt.order_by(Measurement.measured_at.desc(), Measurement.id.desc()).offset((page - 1) * page_size).limit(page_size)
    return {"total": total, "page": page, "page_size": page_size, "items": [_out(m) for m in db.scalars(stmt).all()]}

@router.get("/patients/{patient_id}/measurements/series")
def measurement_series(patient_id: UUID, kind: str = Query(max_length=32), unit: str | None = Query(default=None, max_length=16), start: datetime | None = None, end: datetime | None = None,
                       max_points: int = Query(200, ge=3, le=1000), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    require_scope(db, user, patient_id, AccessScope.MEASUREMENTS)
    kind = kind.upper()
    if kind not in SPECS: raise HTTPException(422, "Unsupported measurement type.")
    if start and end and end < start: raise HTTPException(422, "end cannot precede start.")
    stmt = select(Measurement).where(Measurement.patient_id == patient_id, Measurement.kind == kind)
    if start: stmt = stmt.where(Measurement.measured_at >= start)
    if end: stmt = stmt.where(Measurement.measured_at <= end)
    return build_series(list(db.scalars(stmt).all()), kind, unit=unit, max_points=max_points)

@router.patch("/measurements/{measurement_id}")
def update_measurement(measurement_id: UUID, payload: MeasurementUpdate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    m, _p = resource_patient(db, user, Measurement, measurement_id, scope=AccessScope.MEASUREMENTS, write=True)
    values = payload.model_dump(exclude_unset=True)
    new = {"value": values.get("value", m.value), "value_secondary": values.get("value_secondary", m.value_secondary), "unit": values.get("unit", m.unit),
           "context": values.get("context", m.context), "measured_at": values.get("measured_at", m.measured_at)}
    unit, context = _validated(m.kind, new["value"], new["value_secondary"], new["unit"], new["context"], new["measured_at"])
    m.value, m.value_secondary, m.unit, m.context = new["value"], new["value_secondary"], unit, context
    m.measured_at = new["measured_at"] if new["measured_at"].tzinfo else new["measured_at"].replace(tzinfo=timezone.utc)
    if "note" in values: m.note = values["note"]
    audit(db, user, "MEASUREMENT_UPDATED", "measurement", m.id, context={"kind": m.kind})
    db.commit(); db.refresh(m)
    return _out(m)

@router.delete("/measurements/{measurement_id}", status_code=204)
def delete_measurement(measurement_id: UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    m, _p = resource_patient(db, user, Measurement, measurement_id, scope=AccessScope.MEASUREMENTS, write=True)
    audit(db, user, "MEASUREMENT_DELETED", "measurement", m.id, context={"kind": m.kind})
    db.delete(m); db.commit()
