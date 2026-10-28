from __future__ import annotations
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
import hashlib

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.models import (
    Base, User, PatientProfile, PatientAccess, AccessRole, HealthEvent, EventType,
    Medication, MedicationEvent, MedicationEventType, Investigation, InvestigationResult,
    Document, DocumentStatus, ProcessingJob, Provenance, DatePrecision, AccountStatus,
    AuditLog, EmailVerificationToken,
)
from db.session import get_db
from domain.authorization import patient_for_user, resource_patient


@pytest.fixture
def db():
    engine=create_engine("sqlite://",connect_args={"check_same_thread":False},poolclass=StaticPool)
    @event.listens_for(engine,"connect")
    def _fk(dbapi_connection, connection_record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    Session=sessionmaker(bind=engine,expire_on_commit=False)
    s=Session()
    try: yield s
    finally: s.close()


def user(db,email,verified=True):
    u=User(username=email.split("@")[0],email=email,password_hash="not-a-real-password-hash",email_verified=verified)
    db.add(u); db.commit(); return u


def patient(db,owner):
    p=PatientProfile(owner_user_id=owner.id,first_name="Test",last_name="Patient")
    db.add(p); db.commit(); return p


def test_cross_user_patient_access_denied(db):
    a=user(db,"a@example.com"); b=user(db,"b@example.com"); p=patient(db,b)
    with pytest.raises(HTTPException) as exc: patient_for_user(db,a,p.id)
    assert exc.value.status_code==403


def test_processing_job_cross_user_access_denied(db):
    a=user(db,"a@example.com"); b=user(db,"b@example.com"); p=patient(db,b)
    d=Document(patient_id=p.id,uploaded_by=b.id,filename="r.pdf",content_type="application/pdf",file_size=8,storage_key=f"patients/{p.id}/x",checksum="a"*64,status=DocumentStatus.QUEUED)
    db.add(d); db.flush(); job=ProcessingJob(document_id=d.id,status=DocumentStatus.QUEUED); db.add(job); db.commit()
    from domain.authorization import processing_job_for_user
    with pytest.raises(HTTPException) as exc: processing_job_for_user(db,a,job.id)
    assert exc.value.status_code==403


def test_owner_role_cannot_be_granted(db):
    a=user(db,"a@example.com"); b=user(db,"b@example.com"); p=patient(db,a)
    grant=PatientAccess(patient_id=p.id,user_id=b.id,role=AccessRole.OWNER,granted_by=a.id)
    db.add(grant)
    with pytest.raises(IntegrityError): db.commit()


def test_date_precision_preserved(db):
    a=user(db,"a@example.com"); p=patient(db,a)
    e=HealthEvent(patient_id=p.id,event_type=EventType.CONSULTATION,event_date=date(2022,1,1),date_precision=DatePrecision.YEAR,title="Historical consultation",provenance=Provenance.USER_REPORTED,source_type="MANUAL",created_by=a.id)
    db.add(e); db.commit(); db.refresh(e)
    assert e.event_date==date(2022,1,1)
    assert e.date_precision is DatePrecision.YEAR


def test_invalid_foreign_key_rejected(db):
    a=user(db,"a@example.com")
    e=HealthEvent(patient_id=uuid4(),event_type=EventType.CONSULTATION,title="Broken",provenance=Provenance.USER_REPORTED,source_type="MANUAL",created_by=a.id)
    db.add(e)
    with pytest.raises(IntegrityError): db.commit()


def test_nested_resource_authorization(db):
    a=user(db,"a@example.com"); b=user(db,"b@example.com"); p=patient(db,b)
    e=HealthEvent(patient_id=p.id,event_type=EventType.SURGERY,title="Surgery",provenance=Provenance.USER_REPORTED,source_type="MANUAL",created_by=b.id)
    m=Medication(patient_id=p.id,medicine_name="Drug",provenance=Provenance.USER_REPORTED,created_by=b.id)
    i=Investigation(patient_id=p.id,name="CBC",provenance=Provenance.USER_REPORTED,created_by=b.id)
    db.add_all([e,m,i]); db.flush()
    me=MedicationEvent(medication_id=m.id,event_type=MedicationEventType.STARTED,provenance=Provenance.USER_REPORTED,created_by=b.id)
    r=InvestigationResult(investigation_id=i.id,result_summary="normal",created_by=b.id)
    db.add_all([me,r]); db.commit()
    for model, rid in [(HealthEvent,e.id),(Medication,m.id),(Investigation,i.id)]:
        with pytest.raises(HTTPException) as exc: resource_patient(db,a,model,rid)
        assert exc.value.status_code==403


def _client(db):
    from backend.main import app
    app.dependency_overrides[get_db]=lambda: db
    return TestClient(app)


def _auth_header(user,app):
    from backend.auth import create_access_token
    return {"Authorization":f"Bearer {create_access_token(user)}"}


def test_api_cross_user_resource_matrix_and_revocation(db):
    a=user(db,"a@example.com"); b=user(db,"b@example.com"); pa=patient(db,a); pb=patient(db,b)
    event=HealthEvent(patient_id=pb.id,event_type=EventType.SURGERY,title="B surgery",provenance=Provenance.USER_REPORTED,source_type="MANUAL",created_by=b.id)
    med=Medication(patient_id=pb.id,medicine_name="B drug",provenance=Provenance.USER_REPORTED,created_by=b.id)
    inv=Investigation(patient_id=pb.id,name="B lab",provenance=Provenance.USER_REPORTED,created_by=b.id)
    db.add_all([event,med,inv]); db.flush()
    doc=Document(patient_id=pb.id,uploaded_by=b.id,filename="b.pdf",content_type="application/pdf",file_size=8,storage_key=f"patients/{pb.id}/documents/{uuid4()}/b.pdf",checksum="b"*64,status=DocumentStatus.QUEUED)
    db.add(doc); db.flush(); job=ProcessingJob(document_id=doc.id,status=DocumentStatus.QUEUED); db.add(job); db.commit()
    c=_client(db)
    h=_auth_header(a,c)
    assert c.get(f"/api/patients/{pb.id}",headers=h).status_code==403
    assert c.patch(f"/api/patients/{pb.id}",headers=h,json={"first_name":"Hijack"}).status_code==403
    assert c.delete(f"/api/patients/{pb.id}",headers=h).status_code==403
    assert c.patch(f"/api/events/{event.id}",headers=h,json={"title":"x"}).status_code==403
    assert c.delete(f"/api/events/{event.id}",headers=h).status_code==403
    assert c.post(f"/api/medications/{med.id}/events",headers=h,json={"event_type":"STARTED"}).status_code==403
    assert c.post(f"/api/investigations/{inv.id}/results",headers=h,json={"result_summary":"x"}).status_code==403
    assert c.get(f"/api/documents/{doc.id}",headers=h).status_code==403
    assert c.get(f"/api/documents/{doc.id}/content",headers=h).status_code==403
    assert c.get(f"/api/processing-jobs/{job.id}",headers=h).status_code==403
    assert c.get(f"/api/patients/{pb.id}/events",headers=h).status_code==403
    assert c.get(f"/api/patients/{pb.id}/medications",headers=h).status_code==403
    assert c.get(f"/api/patients/{pb.id}/investigations",headers=h).status_code==403
    assert c.delete(f"/api/patients/{pb.id}/family/{a.id}",headers=h).status_code==403
    assert c.get(f"/api/patients/{uuid4()}",headers=h).status_code==404
    assert c.patch(f"/api/events/{uuid4()}",headers=h,json={"title":"x"}).status_code==404
    assert c.get(f"/api/documents/{uuid4()}",headers=h).status_code==404
    assert c.get(f"/api/processing-jobs/{uuid4()}",headers=h).status_code==404
    assert c.get(f"/api/patients/{pa.id}").status_code==401


def test_family_member_revocation_and_escalation(db):
    owner=user(db,"owner@example.com"); family=user(db,"family@example.com"); outsider=user(db,"outsider@example.com")
    p=patient(db,owner)
    c=_client(db); oh=_auth_header(owner,c); fh=_auth_header(family,c)
    r=c.post(f"/api/patients/{p.id}/family",headers=oh,json={"user_email":family.email,"role":"FAMILY","scopes":["PROFILE","TIMELINE"],"write_scopes":["PROFILE","TIMELINE"]})
    assert r.status_code==201
    assert c.get(f"/api/patients/{p.id}",headers=fh).status_code==200
    updated=c.patch(f"/api/patients/{p.id}",headers=fh,json={"first_name":"FamilyUpdated"})
    assert updated.status_code==200 and updated.json()["owner_user_id"]==str(owner.id)
    assert c.post(f"/api/patients/{p.id}/family",headers=fh,json={"user_email":outsider.email,"role":"FAMILY"}).status_code==403
    assert c.post(f"/api/patients/{p.id}/family",headers=oh,json={"user_email":outsider.email,"role":"OWNER"}).status_code==422
    grant=db.scalar(select(PatientAccess).where(PatientAccess.patient_id==p.id,PatientAccess.user_id==family.id))
    assert grant.role is AccessRole.FAMILY
    assert c.delete(f"/api/patients/{p.id}/family/{family.id}",headers=oh).status_code==204
    assert c.get(f"/api/patients/{p.id}",headers=fh).status_code==403


def test_unauthenticated_and_manipulated_patient_ids(db):
    owner=user(db,"owner2@example.com"); p=patient(db,owner); c=_client(db)
    assert c.get(f"/api/patients/{p.id}").status_code==401
    assert c.patch(f"/api/patients/{p.id}",headers=_auth_header(owner,c),json={"first_name":"Updated"}).status_code==200


def test_provenance_and_actor_are_server_derived(db):
    owner=user(db,"prov-owner@example.com"); family=user(db,"prov-family@example.com"); p=patient(db,owner)
    db.add(PatientAccess(patient_id=p.id,user_id=family.id,role=AccessRole.FAMILY,granted_by=owner.id,scopes=["TIMELINE"],write_scopes=["TIMELINE"])); db.commit()
    c=_client(db)
    malicious={"event_type":"SURGERY","title":"Reported","provenance":"DOCUMENT_VERIFIED","created_by":str(owner.id)}
    out=c.post(f"/api/patients/{p.id}/events",headers=_auth_header(owner,c),json=malicious)
    assert out.status_code==201 and out.json()["provenance"]=="USER_REPORTED" and out.json()["created_by"]==str(owner.id)
    out=c.post(f"/api/patients/{p.id}/events",headers=_auth_header(family,c),json={"event_type":"SURGERY","title":"Family report","provenance":"SYSTEM_DERIVED"})
    assert out.status_code==201 and out.json()["provenance"]=="FAMILY_REPORTED" and out.json()["created_by"]==str(family.id)
    audit_rows=db.scalars(select(AuditLog).where(AuditLog.resource_type=="health_event").order_by(AuditLog.occurred_at)).all()
    assert audit_rows[-1].actor_user_id==family.id


def test_client_cannot_forge_other_provenance_fields(db):
    owner=user(db,"forge@example.com"); p=patient(db,owner); c=_client(db); h=_auth_header(owner,c)
    for payload in [
        {"event_type":"CONSULTATION","title":"x","provenance":"DOCUMENT_VERIFIED"},
        {"event_type":"CONSULTATION","title":"x","provenance":"SYSTEM_DERIVED"},
    ]:
        r=c.post(f"/api/patients/{p.id}/events",headers=h,json=payload)
        assert r.status_code==201 and r.json()["provenance"]=="USER_REPORTED"
    m=c.post(f"/api/patients/{p.id}/medications",headers=h,json={"medicine_name":"A","provenance":"SYSTEM_DERIVED"})
    assert m.status_code==201 and m.json()["provenance"]=="USER_REPORTED"
    i=c.post(f"/api/patients/{p.id}/investigations",headers=h,json={"name":"CBC","provenance":"DOCUMENT_VERIFIED"})
    assert i.status_code==201 and i.json()["provenance"]=="USER_REPORTED"


def test_deleted_and_nonexistent_resources_are_not_authorized(db):
    a=user(db,"deleted@example.com"); p=patient(db,a)
    c=_client(db); h=_auth_header(a,c)
    e=HealthEvent(patient_id=p.id,event_type=EventType.SURGERY,title="x",provenance=Provenance.USER_REPORTED,source_type="MANUAL",created_by=a.id)
    db.add(e); db.commit(); eid=e.id; db.delete(e); db.commit()
    assert c.patch(f"/api/events/{eid}",headers=h,json={"title":"x"}).status_code==404
    assert c.get(f"/api/processing-jobs/{uuid4()}",headers=h).status_code==404


def test_date_precision_update_cannot_become_internally_inconsistent(db):
    owner=user(db,"date-update@example.com"); p=patient(db,owner)
    e=HealthEvent(patient_id=p.id,event_type=EventType.CONSULTATION,event_date=date(2024,1,1),date_precision=DatePrecision.YEAR,title="x",provenance=Provenance.USER_REPORTED,source_type="MANUAL",created_by=owner.id)
    db.add(e); db.commit()
    c=_client(db); h=_auth_header(owner,c)
    assert c.patch(f"/api/events/{e.id}",headers=h,json={"event_date":None}).status_code==422
    assert c.patch(f"/api/events/{e.id}",headers=h,json={"event_date":"2024-03-02","date_precision":"YEAR"}).status_code==422
