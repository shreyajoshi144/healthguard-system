from __future__ import annotations
from datetime import date
from uuid import uuid4
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from db.models import Base, DatePrecision, Document, DocumentStatus, HealthFact, HealthEvent, EventSource, EventType, Investigation, Medication, MedicationEvent, MedicationEventType, MedicationStatus, Provenance, User, PatientProfile
from backend.auth import get_current_user
from backend.main import app
from db.session import get_db
from ai.answer_provider import AnswerClaim, AnswerResult


def make_db():
    e=create_engine('sqlite://',connect_args={'check_same_thread':False},poolclass=StaticPool)
    @event.listens_for(e,'connect')
    def fk(conn,_): conn.execute('PRAGMA foreign_keys=ON')
    Base.metadata.create_all(e); return sessionmaker(bind=e)

def seed(S):
    db=S(); a=User(username=f'a-{uuid4().hex[:6]}',email=f'a-{uuid4().hex[:6]}@x.test',password_hash='x',email_verified=True); b=User(username=f'b-{uuid4().hex[:6]}',email=f'b-{uuid4().hex[:6]}@x.test',password_hash='x',email_verified=True); db.add_all([a,b]); db.flush(); p=PatientProfile(owner_user_id=a.id,first_name='A',last_name='P'); q=PatientProfile(owner_user_id=b.id,first_name='B',last_name='P'); db.add_all([p,q]); db.commit(); return db,a,b,p,q

def client(db,u): app.dependency_overrides[get_db]=lambda:db; app.dependency_overrides[get_current_user]=lambda:u; return TestClient(app)
def clean(): app.dependency_overrides.clear()
def doc(db,p,u,name='record.pdf'):
    d=Document(patient_id=p.id,uploaded_by=u.id,filename=name,content_type='application/pdf',file_size=10,storage_key=f'p/{p.id}/{uuid4()}',checksum=uuid4().hex+'0'*32,status=DocumentStatus.COMPLETED); db.add(d); db.flush(); return d

def fake_provider(monkeypatch,answer='Hemoglobin was 13.2 g/dL.',ids=None):
    class Fake:
        def answer(self,*,question,evidence,system_instructions):
            chosen=ids or [next(x['evidence_id'] for x in evidence if 'Hemoglobin' in str(x.get('value','')))]
            return AnswerResult(answer,[AnswerClaim(answer,chosen)],chosen,True,None)
    monkeypatch.setattr('domain.handoff.get_health_record_answer_provider',lambda:Fake())

def test_handoff_generation_is_draft_and_evidence_backed(monkeypatch):
    S=make_db(); db,u,_,p,_=seed(S); d=doc(db,p,u); f=HealthFact(patient_id=p.id,document_id=d.id,fact_type='LAB_RESULT',value='Hemoglobin 13.2 g/dL',normalized_value='hemoglobin 13.2 g/dl',temporal_date=date(2025,1,1),temporal_precision=DatePrecision.EXACT,source_page=2,source_text='Hemoglobin 13.2 g/dL',confidence=.9,extraction_method='TEST',provenance=Provenance.DOCUMENT_VERIFIED); db.add(f); db.commit(); c=client(db,u); fake_provider(monkeypatch); r=c.post(f'/api/patients/{p.id}/handoffs',json={'title':'New doctor handoff'}); assert r.status_code==201; b=r.json(); assert b['status']=='DRAFT' and b['evidence_refs']; clean(); db.close()

def test_handoff_edit_then_finalize_rejects_unsupported_claim(monkeypatch):
    S=make_db(); db,u,_,p,_=seed(S); d=doc(db,p,u); f=HealthFact(patient_id=p.id,document_id=d.id,fact_type='LAB_RESULT',value='Hemoglobin 13.2 g/dL',normalized_value='hemoglobin 13.2 g/dl',temporal_date=date(2025,1,1),temporal_precision=DatePrecision.EXACT,source_page=2,source_text='Hemoglobin 13.2 g/dL',confidence=.9,extraction_method='TEST',provenance=Provenance.DOCUMENT_VERIFIED); db.add(f); db.commit(); c=client(db,u); fake_provider(monkeypatch); r=c.post(f'/api/patients/{p.id}/handoffs',json={}); hid=r.json()['id']; assert c.patch(f'/api/handoffs/{hid}',json={'content':'The patient has diabetes.'}).status_code==200; assert c.post(f'/api/handoffs/{hid}/finalize').status_code==422; clean(); db.close()

def test_finalized_handoff_is_immutable(monkeypatch):
    S=make_db(); db,u,_,p,_=seed(S); d=doc(db,p,u); f=HealthFact(patient_id=p.id,document_id=d.id,fact_type='LAB_RESULT',value='Hemoglobin 13.2 g/dL',normalized_value='hemoglobin 13.2 g/dl',temporal_date=date(2025,1,1),temporal_precision=DatePrecision.EXACT,source_page=2,source_text='Hemoglobin 13.2 g/dL',confidence=.9,extraction_method='TEST',provenance=Provenance.DOCUMENT_VERIFIED); db.add(f); db.commit(); c=client(db,u); fake_provider(monkeypatch); hid=c.post(f'/api/patients/{p.id}/handoffs',json={}).json()['id']; assert c.post(f'/api/handoffs/{hid}/finalize').status_code==200; assert c.patch(f'/api/handoffs/{hid}',json={'content':'changed'}).status_code==409; clean(); db.close()

def test_handoff_patient_isolation(monkeypatch):
    S=make_db(); db,u,v,p,q=seed(S); c=client(db,v); assert c.get(f'/api/patients/{p.id}/handoffs').status_code==403; clean(); db.close()

def test_manual_action_and_status_lifecycle():
    S=make_db(); db,u,_,p,_=seed(S); c=client(db,u); r=c.post(f'/api/patients/{p.id}/health-actions',json={'title':'Book cardiology follow-up','due_date':'2026-11-10'}); assert r.status_code==201 and r.json()['temporal_precision']=='EXACT'; aid=r.json()['id']; assert c.post(f'/api/health-actions/{aid}/complete').json()['status']=='COMPLETED'; assert c.post(f'/api/health-actions/{aid}/cancel').status_code==409; clean(); db.close()

def test_document_derived_action_preserves_evidence_and_requires_precision():
    S=make_db(); db,u,_,p,_=seed(S); d=doc(db,p,u); db.commit(); c=client(db,u); r=c.post(f'/api/patients/{p.id}/health-actions',json={'title':'Repeat blood test','source':'DOCUMENT_DERIVED','source_document_id':str(d.id),'due_date':'2027-01-15','temporal_precision':'EXACT'}); assert r.status_code==201; b=r.json(); assert b['provenance']=='DOCUMENT_VERIFIED' and b['source_document_id']==str(d.id); clean(); db.close()

def test_document_derived_action_rejects_vague_date_without_confirmation():
    S=make_db(); db,u,_,p,_=seed(S); d=doc(db,p,u); db.commit(); c=client(db,u); r=c.post(f'/api/patients/{p.id}/health-actions',json={'title':'Follow up later','source':'DOCUMENT_DERIVED','source_document_id':str(d.id),'temporal_precision':'UNKNOWN'}); assert r.status_code==201 and r.json()['due_date'] is None; clean(); db.close()

def test_cross_patient_source_reference_rejected():
    S=make_db(); db,u,v,p,q=seed(S); d=doc(db,q,v); db.commit(); c=client(db,u); r=c.post(f'/api/patients/{p.id}/health-actions',json={'title':'Other patient task','source':'DOCUMENT_DERIVED','source_document_id':str(d.id),'temporal_precision':'UNKNOWN'}); assert r.status_code==422; clean(); db.close()

def test_confirm_document_action_is_explicit_not_automatic():
    S=make_db(); db,u,_,p,_=seed(S); d=doc(db,p,u); db.commit(); c=client(db,u); aid=c.post(f'/api/patients/{p.id}/health-actions',json={'title':'Repeat test','source':'DOCUMENT_DERIVED','source_document_id':str(d.id),'due_date':'2027-01-15','temporal_precision':'EXACT'}).json()['id']; assert c.post(f'/api/health-actions/{aid}/confirm').json()['status']=='SCHEDULED'; clean(); db.close()

def test_handoff_includes_medication_investigation_timeline_evidence(monkeypatch):
    S=make_db(); db,u,_,p,_=seed(S); d=doc(db,p,u)
    db.add(Medication(patient_id=p.id,medicine_name='Metformin',dose='500 mg',status=MedicationStatus.ACTIVE,provenance=Provenance.USER_REPORTED,created_by=u.id))
    inv=Investigation(patient_id=p.id,name='MRI brain',performed_date=date(2025,2,1),provenance=Provenance.DOCUMENT_VERIFIED,created_by=u.id); db.add(inv)
    db.add(HealthEvent(patient_id=p.id,event_type=EventType.HOSPITALIZATION,event_date=date(2025,1,1),date_precision=DatePrecision.EXACT,title='Hospitalization',provenance=Provenance.USER_REPORTED,source_type=EventSource.MANUAL,created_by=u.id)); db.commit()
    class Fake:
        def answer(self,*,question,evidence,system_instructions):
            chosen=[evidence[0]['evidence_id']]; text=str(evidence[0].get('value') or evidence[0].get('details'))
            return AnswerResult(text,[AnswerClaim(text,chosen)],chosen,True,None)
    monkeypatch.setattr('domain.handoff.get_health_record_answer_provider',lambda:Fake()); c=client(db,u); r=c.post(f'/api/patients/{p.id}/handoffs',json={}); assert r.status_code==201; types={x['source_type'] for x in r.json()['evidence_refs']}; assert {'MEDICATION','INVESTIGATION','HEALTH_EVENT'} & types; clean(); db.close()

def test_handoff_missing_information_is_explicit():
    S=make_db(); db,u,_,p,_=seed(S); db.commit(); c=client(db,u); r=c.post(f'/api/patients/{p.id}/handoffs',json={}); assert r.status_code==201 and 'unavailable' in r.json()['content'].lower(); clean(); db.close()

def test_handoff_auditability():
    S=make_db(); db,u,_,p,_=seed(S); db.commit(); c=client(db,u); r=c.post(f'/api/patients/{p.id}/handoffs',json={}); assert r.status_code==201; from db.models import AuditLog; logs=db.scalars(select(AuditLog).where(AuditLog.resource_type=='handoff')).all(); assert any(x.action=='HANDOFF_CREATED' for x in logs); clean(); db.close()

def test_approximate_document_action_preserves_precision():
    S=make_db(); db,u,_,p,_=seed(S); d=doc(db,p,u); db.commit(); c=client(db,u); r=c.post(f'/api/patients/{p.id}/health-actions',json={'title':'Repeat test around April','source':'DOCUMENT_DERIVED','source_document_id':str(d.id),'due_date':'2027-04-01','temporal_precision':'APPROXIMATE'}); assert r.status_code==201 and r.json()['temporal_precision']=='APPROXIMATE'; clean(); db.close()

def test_document_action_without_date_does_not_invent_deadline():
    S=make_db(); db,u,_,p,_=seed(S); d=doc(db,p,u); db.commit(); c=client(db,u); r=c.post(f'/api/patients/{p.id}/health-actions',json={'title':'Follow up after some time','source':'DOCUMENT_DERIVED','source_document_id':str(d.id),'temporal_precision':'UNKNOWN'}); assert r.status_code==201 and r.json()['due_date'] is None and r.json()['status']=='PLANNED'; clean(); db.close()

def test_unrelated_new_event_does_not_auto_complete_action():
    S=make_db(); db,u,_,p,_=seed(S); c=client(db,u); aid=c.post(f'/api/patients/{p.id}/health-actions',json={'title':'Repeat blood test'}).json()['id']; db.add(HealthEvent(patient_id=p.id,event_type=EventType.OTHER,event_date=date(2026,5,1),date_precision=DatePrecision.EXACT,title='Unrelated event',provenance=Provenance.USER_REPORTED,source_type=EventSource.MANUAL,created_by=u.id)); db.commit(); action=c.get(f'/api/health-actions/{aid}').json(); assert action['status']=='PLANNED'; clean(); db.close()

def test_action_update_and_cancel_are_patient_scoped():
    S=make_db(); db,u,v,p,q=seed(S); c=client(db,u); aid=c.post(f'/api/patients/{p.id}/health-actions',json={'title':'Follow up'}).json()['id']; c2=client(db,v); assert c2.patch(f'/api/health-actions/{aid}',json={'title':'Hijack'}).status_code==403; c=client(db,u); assert c.patch(f'/api/health-actions/{aid}',json={'title':'Updated'}).status_code==200; assert c.post(f'/api/health-actions/{aid}/cancel').json()['status']=='CANCELLED'; clean(); db.close()

def test_document_followup_extraction_is_suggestion_only_and_preserves_evidence():
    S=make_db(); db,u,_,p,_=seed(S); d=doc(db,p,u); f=HealthFact(patient_id=p.id,document_id=d.id,fact_type='FOLLOW_UP',value='Repeat blood test on 2027-01-15',normalized_value='repeat blood test on 2027-01-15',temporal_date=date(2027,1,15),temporal_precision=DatePrecision.EXACT,source_page=4,source_text='Repeat blood test on 2027-01-15',confidence=.9,extraction_method='TEST',provenance=Provenance.DOCUMENT_VERIFIED); db.add(f); db.commit(); c=client(db,u); r=c.post(f'/api/patients/{p.id}/health-actions/suggest-from-evidence',params={'source_type':'HEALTH_FACT','source_id':str(f.id)}); b=r.json(); assert r.status_code==200 and b['suggested'] and b['requires_user_confirmation'] and b['action']['due_date']=='2027-01-15'; assert c.get(f'/api/patients/{p.id}/health-actions').json()==[]; clean(); db.close()

def test_document_derived_all_reference_types_are_document_verified():
    S=make_db(); db,u,_,p,_=seed(S); d=doc(db,p,u)
    fact=HealthFact(patient_id=p.id,document_id=d.id,fact_type='FOLLOW_UP',value='Repeat test',normalized_value='repeat test',source_page=1,source_text='Repeat test',confidence=.9,extraction_method='TEST',provenance=Provenance.DOCUMENT_VERIFIED)
    event=HealthEvent(patient_id=p.id,event_type=EventType.FOLLOW_UP,event_date=date(2027,1,15),date_precision=DatePrecision.EXACT,title='Follow-up',provenance=Provenance.DOCUMENT_VERIFIED,source_type=EventSource.DOCUMENT,source_id=d.id,created_by=u.id)
    investigation=Investigation(patient_id=p.id,name='Blood test',performed_date=date(2027,1,15),provenance=Provenance.DOCUMENT_VERIFIED,created_by=u.id)
    medication=Medication(patient_id=p.id,medicine_name='TestMed',provenance=Provenance.DOCUMENT_VERIFIED,source_id=d.id,created_by=u.id)
    db.add_all([fact,event,investigation,medication]); db.flush()
    med_event=MedicationEvent(medication_id=medication.id,event_type=MedicationEventType.STARTED,event_date=date(2027,1,15),provenance=Provenance.DOCUMENT_VERIFIED,created_by=u.id)
    db.add(med_event); db.commit()
    c=client(db,u)
    payload={'title':'Document-backed follow-up','source':'DOCUMENT_DERIVED','source_document_id':str(d.id),'source_fact_id':str(fact.id),'source_event_id':str(event.id),'source_investigation_id':str(investigation.id),'source_medication_event_id':str(med_event.id),'temporal_precision':'EXACT','due_date':'2027-01-15'}
    r=c.post(f'/api/patients/{p.id}/health-actions',json=payload)
    assert r.status_code==201 and r.json()['provenance']=='DOCUMENT_VERIFIED'
    clean(); db.close()


def test_document_derived_cannot_upgrade_user_or_family_sources():
    S=make_db(); db,u,v,p,_=seed(S); d=doc(db,p,u)
    fact=HealthFact(patient_id=p.id,document_id=d.id,fact_type='FOLLOW_UP',value='User follow-up',normalized_value='user follow-up',source_page=1,source_text='User follow-up',confidence=.9,extraction_method='TEST',provenance=Provenance.USER_REPORTED)
    event=HealthEvent(patient_id=p.id,event_type=EventType.FOLLOW_UP,event_date=date(2027,1,15),date_precision=DatePrecision.EXACT,title='Family follow-up',provenance=Provenance.FAMILY_REPORTED,source_type=EventSource.MANUAL,created_by=v.id)
    investigation=Investigation(patient_id=p.id,name='Family test',provenance=Provenance.FAMILY_REPORTED,created_by=v.id)
    medication=Medication(patient_id=p.id,medicine_name='UserMed',provenance=Provenance.USER_REPORTED,created_by=u.id)
    db.add_all([fact,event,investigation,medication]); db.flush()
    med_event=MedicationEvent(medication_id=medication.id,event_type=MedicationEventType.STARTED,provenance=Provenance.USER_REPORTED,created_by=u.id)
    db.add(med_event); db.commit(); c=client(db,u)
    refs=[('source_fact_id',fact.id),('source_event_id',event.id),('source_investigation_id',investigation.id),('source_medication_event_id',med_event.id)]
    for key,ref in refs:
        r=c.post(f'/api/patients/{p.id}/health-actions',json={'title':'Attempt upgrade','source':'DOCUMENT_DERIVED',key:str(ref),'temporal_precision':'UNKNOWN'})
        assert r.status_code==422, key
    clean(); db.close()


def test_document_derived_cross_patient_reference_stays_rejected_for_all_reference_types():
    S=make_db(); db,u,v,p,q=seed(S); dp=doc(db,p,u); dq=doc(db,q,v)
    qfact=HealthFact(patient_id=q.id,document_id=dq.id,fact_type='FOLLOW_UP',value='q',normalized_value='q',source_page=1,source_text='q',confidence=.9,extraction_method='TEST',provenance=Provenance.DOCUMENT_VERIFIED)
    qevent=HealthEvent(patient_id=q.id,event_type=EventType.FOLLOW_UP,title='q',provenance=Provenance.DOCUMENT_VERIFIED,source_type=EventSource.DOCUMENT,source_id=dq.id,created_by=v.id)
    qinv=Investigation(patient_id=q.id,name='q',provenance=Provenance.DOCUMENT_VERIFIED,created_by=v.id)
    qmed=Medication(patient_id=q.id,medicine_name='q',provenance=Provenance.DOCUMENT_VERIFIED,source_id=dq.id,created_by=v.id)
    db.add_all([qfact,qevent,qinv,qmed]); db.flush(); qme=MedicationEvent(medication_id=qmed.id,event_type=MedicationEventType.STARTED,provenance=Provenance.DOCUMENT_VERIFIED,created_by=v.id); db.add(qme); db.commit(); c=client(db,u)
    refs=[('source_document_id',dq.id),('source_fact_id',qfact.id),('source_event_id',qevent.id),('source_investigation_id',qinv.id),('source_medication_event_id',qme.id)]
    for key,ref in refs:
        r=c.post(f'/api/patients/{p.id}/health-actions',json={'title':'Cross patient','source':'DOCUMENT_DERIVED',key:str(ref),'temporal_precision':'UNKNOWN'})
        assert r.status_code==422, key
    clean(); db.close()
