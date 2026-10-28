from __future__ import annotations
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from db.models import Base, User, PatientProfile, PatientAccess, AccessRole, AccessGrantStatus, AccessScope, Medication, MedicationStatus, HealthEvent, EventType, Document, DocumentStatus, Handoff, HandoffStatus, HealthAction, HealthActionStatus, AuditLog
from backend.main import app
from backend.auth import get_current_user, create_access_token
from db.session import get_db


def make_db():
    e=create_engine('sqlite://',connect_args={'check_same_thread':False},poolclass=StaticPool)
    @event.listens_for(e,'connect')
    def fk(conn,_): conn.execute('PRAGMA foreign_keys=ON')
    Base.metadata.create_all(e); return sessionmaker(bind=e)

def seed(S):
    db=S(); a=User(username=f'a-{uuid4().hex[:6]}',email=f'a-{uuid4().hex[:6]}@example.com',password_hash='x',email_verified=True); b=User(username=f'b-{uuid4().hex[:6]}',email=f'b-{uuid4().hex[:6]}@example.com',password_hash='x',email_verified=True); c=User(username=f'c-{uuid4().hex[:6]}',email=f'c-{uuid4().hex[:6]}@example.com',password_hash='x',email_verified=True); db.add_all([a,b,c]); db.flush(); p=PatientProfile(owner_user_id=a.id,first_name='A',last_name='Patient'); q=PatientProfile(owner_user_id=b.id,first_name='B',last_name='Patient'); db.add_all([p,q]); db.commit(); return db,a,b,c,p,q

def client(db,u): app.dependency_overrides[get_db]=lambda:db; app.dependency_overrides[get_current_user]=lambda:u; return TestClient(app)
def grant(db,owner,p,user,scopes,write=(),role=AccessRole.FAMILY,expires=None):
    g=PatientAccess(patient_id=p.id,user_id=user.id,role=role,granted_by=owner.id,status=AccessGrantStatus.ACTIVE,scopes=list(scopes),write_scopes=list(write),expires_at=expires); db.add(g); db.commit(); return g
def clean(): app.dependency_overrides.clear()


def test_every_scope_read_boundary_is_enforced_at_api():
    S=make_db(); db,a,b,c,p,q=seed(S)
    med=Medication(patient_id=p.id,medicine_name='A',status=MedicationStatus.ACTIVE,created_by=a.id); ev=HealthEvent(patient_id=p.id,event_type=EventType.OTHER,title='E',created_by=a.id); doc=Document(patient_id=p.id,uploaded_by=a.id,filename='x.pdf',content_type='application/pdf',file_size=1,storage_key='x',checksum='1'*64,status=DocumentStatus.COMPLETED); hand=Handoff(patient_id=p.id,title='H',handoff_type='DOCTOR_HANDOFF',status=HandoffStatus.FINAL,content='x',evidence_refs=[]); act=HealthAction(patient_id=p.id,title='A',action_type='FOLLOW_UP',source='USER_CREATED',provenance='USER_REPORTED',status=HealthActionStatus.PLANNED,temporal_precision='UNKNOWN'); db.add_all([med,ev,doc,hand,act]); db.commit()
    g=grant(db,a,p,b,['PROFILE','DOCUMENTS','MEDICATIONS','TIMELINE','HANDOFFS','HEALTH_ACTIONS'])
    u=client(db,b)
    assert u.get(f'/api/patients/{p.id}').status_code==200
    assert u.get(f'/api/patients/{p.id}/documents').status_code==200
    assert u.get(f'/api/patients/{p.id}/medications').status_code==200
    assert u.get(f'/api/patients/{p.id}/events').status_code==200
    assert u.get(f'/api/patients/{p.id}/handoffs').status_code==200
    assert u.get(f'/api/patients/{p.id}/health-actions').status_code==200
    g.scopes=['PROFILE']; db.commit()
    assert u.get(f'/api/patients/{p.id}/documents').status_code==403
    assert u.get(f'/api/patients/{p.id}/medications').status_code==403
    assert u.get(f'/api/patients/{p.id}/events').status_code==403
    assert u.get(f'/api/patients/{p.id}/handoffs').status_code==403
    assert u.get(f'/api/patients/{p.id}/health-actions').status_code==403
    clean(); db.close()

def test_cross_patient_resource_ids_cannot_be_used_for_reads_or_writes():
    S=make_db(); db,a,b,c,p,q=seed(S)
    m=Medication(patient_id=q.id,medicine_name='B',status=MedicationStatus.ACTIVE,created_by=b.id); e=HealthEvent(patient_id=q.id,event_type=EventType.OTHER,title='B',created_by=b.id); d=Document(patient_id=q.id,uploaded_by=b.id,filename='b.pdf',content_type='application/pdf',file_size=1,storage_key='b',checksum='2'*64,status=DocumentStatus.COMPLETED); h=Handoff(patient_id=q.id,title='B',handoff_type='DOCTOR_HANDOFF',status=HandoffStatus.FINAL,content='b',evidence_refs=[]); a2=HealthAction(patient_id=q.id,title='B',action_type='FOLLOW_UP',source='USER_CREATED',provenance='USER_REPORTED',status=HealthActionStatus.PLANNED,temporal_precision='UNKNOWN'); db.add_all([m,e,d,h,a2]); db.commit(); grant(db,b,q,c,['MEDICATIONS','TIMELINE','DOCUMENTS','HANDOFFS','HEALTH_ACTIONS'],write=['MEDICATIONS','TIMELINE','DOCUMENTS','HANDOFFS','HEALTH_ACTIONS'])
    u=client(db,c)
    assert u.get(f'/api/medications/{m.id}/events').status_code==200
    assert u.delete(f'/api/events/{e.id}').status_code==204
    assert u.get(f'/api/documents/{d.id}').status_code==200
    assert u.get(f'/api/handoffs/{h.id}').status_code==200
    assert u.get(f'/api/health-actions/{a2.id}').status_code==200
    # c has access to q, not p; references to p must fail.
    mp=Medication(patient_id=p.id,medicine_name='A',status=MedicationStatus.ACTIVE,created_by=a.id)
    ep=HealthEvent(patient_id=p.id,event_type=EventType.OTHER,title='A',created_by=a.id)
    dp=Document(patient_id=p.id,uploaded_by=a.id,filename='a.pdf',content_type='application/pdf',file_size=1,storage_key='a',checksum='4'*64,status=DocumentStatus.COMPLETED)
    hp=Handoff(patient_id=p.id,title='A',handoff_type='DOCTOR_HANDOFF',status=HandoffStatus.FINAL,content='a',evidence_refs=[])
    ap=HealthAction(patient_id=p.id,title='A',action_type='FOLLOW_UP',source='USER_CREATED',provenance='USER_REPORTED',status=HealthActionStatus.PLANNED,temporal_precision='UNKNOWN')
    db.add_all([mp,ep,dp,hp,ap]); db.commit()
    assert u.get(f'/api/medications/{mp.id}/events').status_code==403
    assert u.delete(f'/api/events/{ep.id}').status_code==403
    assert u.get(f'/api/documents/{dp.id}').status_code==403
    assert u.get(f'/api/handoffs/{hp.id}').status_code==403
    assert u.get(f'/api/health-actions/{ap.id}').status_code==403
    clean(); db.close()


def test_logout_revokes_existing_session_token():
    S=make_db(); db,a,b,c,p,q=seed(S); token=create_access_token(a); app.dependency_overrides.clear(); app.dependency_overrides[get_db]=lambda:db; c1=TestClient(app); c1.cookies.set('hg_session',token); c1.cookies.set('hg_csrf','csrf'); r=c1.post('/api/auth/logout',headers={'X-CSRF-Token':'csrf'}); assert r.status_code==204
    app.dependency_overrides[get_current_user].__class__ if False else None
    # Exercise actual token validation directly through a protected endpoint.
    c2=TestClient(app); c2.cookies.set('hg_session',token); assert c2.get('/api/auth/me').status_code==401; clean(); db.close()


def test_ai_audit_does_not_store_raw_question(monkeypatch):
    S=make_db(); db,a,b,c,p,q=seed(S); from db.models import HealthFact, Provenance
    d=Document(patient_id=p.id,uploaded_by=a.id,filename='f.pdf',content_type='application/pdf',file_size=1,storage_key='f',checksum='3'*64,status=DocumentStatus.COMPLETED); db.add(d); db.flush(); f=__import__('db.models',fromlist=['HealthFact']).HealthFact(patient_id=p.id,document_id=d.id,fact_type='LAB',value='100',source_page=1,source_text='glucose 100',extraction_method='TEST',provenance=Provenance.DOCUMENT_VERIFIED); db.add(f); db.commit()
    from ai.answer_provider import AnswerResult,AnswerClaim
    class Fake:
        def answer(self,**kwargs): return AnswerResult('Glucose 100.',[AnswerClaim('Glucose 100.',['evidence_1'])],['evidence_1'],True,None)
    monkeypatch.setattr('ai.answer_provider.get_health_record_answer_provider',lambda:Fake())
    r=client(db,a).post(f'/api/patients/{p.id}/ask',json={'question':'My secret diagnosis is diabetes; what was glucose?' }); assert r.status_code==200
    row=db.scalars(select(AuditLog).where(AuditLog.action=='HEALTH_RECORD_ANSWER')).first(); assert row and row.context['question']=='[REDACTED]' and len(row.context['question_sha256'])==64; assert 'diabetes' not in str(row.context); clean(); db.close()


def test_temporary_share_rejects_profile_resource_ids_and_unknown_scopes():
    S=make_db(); db,a,b,c,p,q=seed(S); owner=client(db,a); exp=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()
    assert owner.post(f'/api/patients/{p.id}/shares',json={'scopes':['PROFILE'],'resources':{'PROFILE':[str(p.id)]},'expires_at':exp}).status_code==422
    assert owner.post(f'/api/patients/{p.id}/shares',json={'scopes':['DOCUMENTS'],'resources':{'NOT_A_SCOPE':[str(uuid4())]},'expires_at':exp}).status_code==422; clean(); db.close()
