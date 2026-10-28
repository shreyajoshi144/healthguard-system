from __future__ import annotations
import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.models import Base, User, PatientProfile, HealthEvent, EventType, Provenance, Document, DocumentStatus, ProcessingJob, PatientAccess, AccessRole, AuditLog
from db.session import get_db
from backend.auth import create_access_token

@pytest.fixture
def db():
    engine=create_engine("sqlite://",connect_args={"check_same_thread":False},poolclass=StaticPool)
    @event.listens_for(engine,"connect")
    def _fk(conn,_): conn.execute("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    Session=sessionmaker(bind=engine,expire_on_commit=False)
    s=Session()
    try: yield s
    finally: s.close()

def user(db,email):
    u=User(username=email.split('@')[0],email=email,password_hash='x',email_verified=True); db.add(u); db.commit(); return u

def patient(db,u):
    p=PatientProfile(owner_user_id=u.id,first_name='A',last_name='Patient'); db.add(p); db.commit(); return p

def client(db):
    from backend.main import app
    app.dependency_overrides[get_db]=lambda:db
    return TestClient(app)

def h(u): return {"Authorization":f"Bearer {create_access_token(u)}"}

def test_pagination_bounds_and_deterministic_order(db):
    u=user(db,'page@example.com'); p=patient(db,u); c=client(db)
    for n in range(3):
        db.add(HealthEvent(patient_id=p.id,event_type=EventType.CONSULTATION,title=f'e{n}',provenance=Provenance.USER_REPORTED,source_type='MANUAL',created_by=u.id))
    db.commit()
    r=c.get(f'/api/patients/{p.id}/events?page=1&page_size=2',headers=h(u)); assert r.status_code==200 and len(r.json())==2
    assert c.get(f'/api/patients/{p.id}/events?page=0',headers=h(u)).status_code==422
    assert c.get(f'/api/patients/{p.id}/events?page_size=101',headers=h(u)).status_code==422
    assert len(c.get(f'/api/patients/{p.id}/events?page=2&page_size=2',headers=h(u)).json())==1

def test_validation_rejects_oversized_text(db):
    u=user(db,'validation@example.com'); p=patient(db,u); c=client(db)
    r=c.post(f'/api/patients/{p.id}/medications',headers=h(u),json={'medicine_name':'A','notes':'x'*10001})
    assert r.status_code==422 and r.json()=={'detail':'Invalid request.'}

def test_phi_safe_validation_error(db):
    u=user(db,'phi@example.com'); c=client(db)
    secret='SSN-123-45-6789'
    r=c.post('/api/patients',headers=h(u),json={'first_name':secret,'last_name':'B','unexpected':secret})
    assert r.status_code in (201,422)
    if r.status_code==422: assert secret not in r.text

def test_production_rate_limit_fails_closed(monkeypatch):
    import domain.rate_limit as rl
    class BrokenRedis:
        def incr(self,key): raise RuntimeError('redis down')
    monkeypatch.setenv('APP_ENV','production'); monkeypatch.setenv('REDIS_URL','redis://broken')
    monkeypatch.setattr(rl,'redis',type('R',(),{'from_url':lambda *a,**k:BrokenRedis()}))
    dep=rl.rate_limit('phase5',1)
    from fastapi import Request
    class Client: host='127.0.0.1'
    class Req: client=Client()
    with pytest.raises(Exception) as exc: asyncio.run(dep(Req()))
    assert getattr(exc.value,'status_code',None)==503

def test_verification_token_missing_is_controlled(monkeypatch,db):
    from backend import auth
    with pytest.raises(auth.VerificationError): auth.verify_email_token(db,'does-not-exist')

def test_cors_is_explicit_and_headers_are_hardened(db,monkeypatch):
    from backend.main import app
    u=user(db,'cors@example.com'); app.dependency_overrides[get_db]=lambda:db
    c=TestClient(app)
    r=c.get('/api/health',headers={'Origin':'http://localhost:8000'})
    assert r.status_code==200
    assert r.headers.get('access-control-allow-origin')=='http://localhost:8000'
    assert r.headers.get('x-content-type-options')=='nosniff'
    assert r.headers.get('x-frame-options')=='DENY'
    assert r.headers.get('referrer-policy')=='no-referrer'

def test_processing_job_status_is_scoped_to_document_owner(db):
    owner=user(db,'job-owner@example.com'); other=user(db,'job-other@example.com'); p=patient(db,owner)
    d=Document(patient_id=p.id,uploaded_by=owner.id,filename='x.pdf',content_type='application/pdf',file_size=8,storage_key=f'p/{uuid4()}',checksum='1'*64,status=DocumentStatus.QUEUED)
    db.add(d); db.flush(); j=ProcessingJob(document_id=d.id,status=DocumentStatus.QUEUED); db.add(j); db.commit()
    c=client(db)
    assert c.get(f'/api/processing-jobs/{j.id}',headers=h(other)).status_code==403
    assert c.get(f'/api/processing-jobs/{j.id}',headers=h(owner)).status_code==200

def test_family_grant_duplicate_is_conflict(db):
    owner=user(db,'grant-owner@example.com'); family=user(db,'grant-family@example.com'); p=patient(db,owner); c=client(db)
    assert c.post(f'/api/patients/{p.id}/family',headers=h(owner),json={'user_email':family.email,'role':'FAMILY'}).status_code==201
    assert c.post(f'/api/patients/{p.id}/family',headers=h(owner),json={'user_email':family.email,'role':'FAMILY'}).status_code==409

def test_audit_does_not_store_request_body_or_tokens(db):
    u=user(db,'audit-safe@example.com'); p=patient(db,u); c=client(db)
    token='SECRET-VERIFICATION-TOKEN'
    r=c.post(f'/api/patients/{p.id}/events',headers=h(u),json={'event_type':'SURGERY','title':'Private medical detail','description':token})
    assert r.status_code==201
    row=db.scalar(select(AuditLog).where(AuditLog.resource_type=='health_event').order_by(AuditLog.occurred_at.desc()))
    assert row.actor_user_id==u.id
    assert token not in str(row.context or {})
    assert 'Private medical detail' not in str(row.context or {})

def test_complete_sprint1_user_journey(monkeypatch,db,tmp_path):
    from backend.main import app
    from storage.service import Storage
    sent=[]
    monkeypatch.setattr('backend.main.send_verification_email',lambda user,token:sent.append(token))
    s=Storage(); s.backend='local'; s.root=Path(tmp_path)
    monkeypatch.setattr('api.healthguard.storage',s)
    app.dependency_overrides[get_db]=lambda:db
    c=TestClient(app)

    r=c.post('/api/auth/register',json={'username':'journeyowner','email':'journey@example.com','password':'correct horse battery staple'})
    assert r.status_code==202 and sent
    assert c.post('/api/auth/login',json={'username':'journey@example.com','password':'correct horse battery staple'}).status_code==401
    assert c.get('/api/auth/verify-email',params={'token':sent[0]},follow_redirects=False).status_code==303
    login=c.post('/api/auth/login',json={'username':'journey@example.com','password':'correct horse battery staple'})
    assert login.status_code==200
    assert 'access_token' not in login.json()
    assert c.cookies.get('hg_session')
    bearer={'X-CSRF-Token': c.cookies.get('hg_csrf')}

    p=c.post('/api/patients',headers=bearer,json={'first_name':'Jane','last_name':'Doe'}); assert p.status_code==201
    pid=p.json()['id']
    assert c.post(f'/api/patients/{pid}/events',headers=bearer,json={'event_type':'SURGERY','title':'History'}).status_code==201
    m=c.post(f'/api/patients/{pid}/medications',headers=bearer,json={'medicine_name':'Medicine'}); assert m.status_code==201
    assert c.post(f"/api/medications/{m.json()['id']}/events",headers=bearer,json={'event_type':'STARTED'}).status_code==201
    inv=c.post(f'/api/patients/{pid}/investigations',headers=bearer,json={'name':'CBC'}); assert inv.status_code==201
    assert c.post(f"/api/investigations/{inv.json()['id']}/results",headers=bearer,json={'result_summary':'Normal'}).status_code==201
    upload=c.post(f'/api/patients/{pid}/documents',headers=bearer,files={'file':('lab.pdf',b'%PDF-1.7 test','application/pdf')}); assert upload.status_code==201
    did=upload.json()['id']
    assert c.get(f'/api/documents/{did}',headers=bearer).status_code==200

    family=User(username='journeyfamily',email='family-journey@example.com',password_hash='x',email_verified=True); db.add(family); db.commit()
    assert c.post(f'/api/patients/{pid}/family',headers=bearer,json={'user_email':family.email,'role':'FAMILY'}).status_code==201
    family_h={'Authorization':f'Bearer {create_access_token(family)}'}
    assert c.get(f'/api/patients/{pid}',headers=family_h).status_code==200
    assert c.delete(f'/api/patients/{pid}/family/{family.id}',headers=bearer).status_code==204
    assert c.get(f'/api/patients/{pid}',headers=family_h).status_code==403
    assert c.delete(f'/api/documents/{did}',headers=bearer).status_code==204
    assert c.get(f'/api/documents/{did}',headers=bearer).status_code==404

    csrf=c.cookies.get('hg_csrf')
    assert csrf and c.post('/api/auth/logout',headers={'X-CSRF-Token':csrf}).status_code==204


def test_login_response_does_not_expose_access_token(db):
    from backend import auth
    from backend.main import app
    auth.register_user(db,"browserlogin","browserlogin@example.com","correct horse battery staple").email_verified=True
    db.commit()
    app.dependency_overrides[get_db]=lambda:db
    c=TestClient(app)
    r=c.post("/api/auth/login",json={"username":"browserlogin@example.com","password":"correct horse battery staple"})
    assert r.status_code==200
    assert "access_token" not in r.json()
    assert c.cookies.get("hg_session")
    assert c.get("/api/auth/me").status_code==200
