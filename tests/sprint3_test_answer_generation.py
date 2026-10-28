from __future__ import annotations
from datetime import date
from uuid import uuid4
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from db.models import Base, DatePrecision, Document, DocumentStatus, HealthFact, Investigation, InvestigationResult, Medication, MedicationStatus, PatientProfile, Provenance, User
from backend.auth import get_current_user
from backend.main import app
from db.session import get_db
from ai.answer_provider import AnswerClaim, AnswerResult, AnswerProviderResponseError


def make_db():
    e=create_engine("sqlite://",connect_args={"check_same_thread":False},poolclass=StaticPool)
    @event.listens_for(e,"connect")
    def fk(conn,_): conn.execute("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(e); return sessionmaker(bind=e)

def seed(S):
    db=S(); a=User(username=f"a-{uuid4().hex[:6]}",email=f"a-{uuid4().hex[:6]}@x.test",password_hash="x",email_verified=True); b=User(username=f"b-{uuid4().hex[:6]}",email=f"b-{uuid4().hex[:6]}@x.test",password_hash="x",email_verified=True); db.add_all([a,b]); db.flush(); p=PatientProfile(owner_user_id=a.id,first_name="A",last_name="P"); q=PatientProfile(owner_user_id=b.id,first_name="B",last_name="P"); db.add_all([p,q]); db.flush(); return db,a,b,p,q

def doc(db,p,u,name):
    d=Document(patient_id=p.id,uploaded_by=u.id,filename=name,content_type="application/pdf",file_size=10,storage_key=f"p/{p.id}/{uuid4()}",checksum=uuid4().hex+"0"*32,status=DocumentStatus.COMPLETED); db.add(d); db.flush(); return d

def fact(db,p,d,value,when=date(2025,1,2),ft="LAB_RESULT",prov=Provenance.DOCUMENT_VERIFIED):
    f=HealthFact(patient_id=p.id,document_id=d.id,fact_type=ft,value=value,normalized_value=value.lower(),temporal_date=when,temporal_precision=DatePrecision.EXACT,source_page=3,source_text=f"Exact evidence: {value}",confidence=.9,extraction_method="TEST",provenance=prov); db.add(f); db.flush(); return f

def client(db,u):
    app.dependency_overrides[get_db]=lambda:db; app.dependency_overrides[get_current_user]=lambda:u; return TestClient(app)

def clean(): app.dependency_overrides.clear()

def fake_result(answer, ids, grounded=True, claims=None):
    claims=claims or [AnswerClaim(answer,ids)]
    return AnswerResult(answer=answer,claims=claims,citations=ids,grounded=grounded,uncertainty=None)

def patch_provider(monkeypatch, result=None, error=None, capture=None):
    class Fake:
        def answer(self, *, question, evidence, system_instructions):
            if capture is not None: capture.append((question,evidence,system_instructions))
            if error: raise error
            return result
    monkeypatch.setattr("ai.answer_provider.get_health_record_answer_provider", lambda: Fake())

def test_grounded_medication_question(monkeypatch):
    S=make_db(); db,u,_,p,_=seed(S); m=Medication(patient_id=p.id,medicine_name="Metformin",dose="500 mg",status=MedicationStatus.ACTIVE,provenance=Provenance.USER_REPORTED,created_by=u.id); db.add(m); db.commit(); c=client(db,u)
    captured=[]; patch_provider(monkeypatch,fake_result("The record lists Metformin 500 mg.",["evidence_1"]),capture=captured)
    r=c.post(f"/api/patients/{p.id}/ask",json={"question":"What is the Metformin dose?"}); assert r.status_code==200; body=r.json(); assert body["grounded"] and body["citations"]==["evidence_1"]; assert "patient_id" not in captured[0][1][0]
    clean(); db.close()

def test_grounded_lab_question_and_exact_evidence(monkeypatch):
    S=make_db(); db,u,_,p,_=seed(S); d=doc(db,p,u,"lab.pdf"); f=fact(db,p,d,"Hemoglobin 13.2 g/dL"); db.commit(); c=client(db,u); patch_provider(monkeypatch,fake_result("The record documents hemoglobin 13.2 g/dL.",["evidence_1"])); r=c.post(f"/api/patients/{p.id}/ask",json={"question":"What was the hemoglobin?"}); b=r.json(); assert b["grounded"] and b["evidence"][0]["source_text"]==f.source_text and b["evidence"][0]["provenance"]=="DOCUMENT_VERIFIED"; clean(); db.close()

def test_grounded_investigation_question(monkeypatch):
    S=make_db(); db,u,_,p,_=seed(S); i=Investigation(patient_id=p.id,name="MRI brain",performed_date=date(2025,2,1),provenance=Provenance.USER_REPORTED,created_by=u.id); db.add(i); db.flush(); r0=InvestigationResult(investigation_id=i.id,result_summary="No acute findings",result_date=date(2025,2,2),created_by=u.id); db.add(r0); db.commit(); c=client(db,u); patch_provider(monkeypatch,fake_result("The MRI result says no acute findings.",["evidence_1","evidence_2"],claims=[AnswerClaim("The MRI result says no acute findings.",["evidence_1","evidence_2"])])); r=c.post(f"/api/patients/{p.id}/ask",json={"question":"What did the MRI show?"}); assert r.json()["grounded"]; clean(); db.close()

def test_date_specific_question(monkeypatch):
    S=make_db(); db,u,_,p,_=seed(S); d1=doc(db,p,u,"old.pdf"); d2=doc(db,p,u,"new.pdf"); fact(db,p,d1,"Glucose 100 mg/dL",date(2024,5,1)); fact(db,p,d2,"Glucose 110 mg/dL",date(2025,5,1)); db.commit(); c=client(db,u); patch_provider(monkeypatch,fake_result("The 2025 record shows glucose 110 mg/dL.",["evidence_1"])); r=c.post(f"/api/patients/{p.id}/ask",json={"question":"What was glucose in 2025?","date_from":"2025-01-01","date_to":"2025-12-31"}); assert r.json()["grounded"] and len(r.json()["evidence"])==1; clean(); db.close()

def test_multiple_source_answer(monkeypatch):
    S=make_db(); db,u,_,p,_=seed(S); d=doc(db,p,u,"lab.pdf"); fact(db,p,d,"Vitamin D 20 ng/mL"); m=Medication(patient_id=p.id,medicine_name="Metformin",dose="500 mg",status=MedicationStatus.ACTIVE,provenance=Provenance.USER_REPORTED,created_by=u.id); db.add(m); db.commit(); c=client(db,u); patch_provider(monkeypatch,fake_result("The record lists Vitamin D 20 ng/mL and Metformin 500 mg.",["evidence_1","evidence_2"],claims=[AnswerClaim("The record lists Vitamin D 20 ng/mL.",["evidence_1"]),AnswerClaim("The record lists Metformin 500 mg.",["evidence_2"])])); r=c.post(f"/api/patients/{p.id}/ask",json={"question":"Summarize Vitamin D and Metformin"}); assert r.json()["citations"]==["evidence_1","evidence_2"]; clean(); db.close()

def test_no_evidence_returns_insufficient(monkeypatch):
    S=make_db(); db,u,_,p,_=seed(S); db.commit(); c=client(db,u); called=[]; patch_provider(monkeypatch,fake_result("bad",["evidence_1"]),capture=called); r=c.post(f"/api/patients/{p.id}/ask",json={"question":"What is my diagnosis?"}); assert r.status_code==200 and not r.json()["grounded"] and not called; clean(); db.close()

def test_insufficient_provider_response_is_safe(monkeypatch):
    S=make_db(); db,u,_,p,_=seed(S); d=doc(db,p,u,"lab.pdf"); fact(db,p,d,"Vitamin D 20 ng/mL"); db.commit(); c=client(db,u); patch_provider(monkeypatch,AnswerResult("I don't know",[],[],False,"insufficient")); r=c.post(f"/api/patients/{p.id}/ask",json={"question":"What is my blood type?"}); assert not r.json()["grounded"] and r.json()["citations"]==[]; clean(); db.close()

def test_invalid_citation_id_rejected(monkeypatch):
    S=make_db(); db,u,_,p,_=seed(S); d=doc(db,p,u,"lab.pdf"); fact(db,p,d,"Glucose 100 mg/dL"); db.commit(); c=client(db,u); patch_provider(monkeypatch,fake_result("Glucose was 100 mg/dL.",["evidence_999"])); r=c.post(f"/api/patients/{p.id}/ask",json={"question":"What was glucose?"}); assert not r.json()["grounded"] and r.json()["citations"]==[]; clean(); db.close()

def test_citation_from_another_patient_rejected(monkeypatch):
    S=make_db(); db,u,v,p,q=seed(S); d=doc(db,p,u,"lab.pdf"); fact(db,p,d,"Glucose 100 mg/dL"); d2=doc(db,q,v,"secret.pdf"); fact(db,q,d2,"Glucose 999 mg/dL"); db.commit(); c=client(db,u); patch_provider(monkeypatch,fake_result("Glucose was 999 mg/dL.",["evidence_2"])); r=c.post(f"/api/patients/{p.id}/ask",json={"question":"What was glucose?"}); assert not r.json()["grounded"] and r.json()["citations"]==[]; clean(); db.close()

def test_contradictory_facts_require_both_citations(monkeypatch):
    S=make_db(); db,u,_,p,_=seed(S); d1=doc(db,p,u,"a.pdf"); d2=doc(db,p,u,"b.pdf"); fact(db,p,d1,"Hemoglobin 13.2 g/dL"); fact(db,p,d2,"Hemoglobin 10.1 g/dL"); db.commit(); c=client(db,u); patch_provider(monkeypatch,fake_result("Hemoglobin was 13.2 g/dL.",["evidence_1"])); r=c.post(f"/api/patients/{p.id}/ask",json={"question":"What are the hemoglobin results?"}); assert not r.json()["grounded"]; clean(); db.close()

def test_provenance_preserved(monkeypatch):
    S=make_db(); db,u,_,p,_=seed(S); d=doc(db,p,u,"record.pdf"); fact(db,p,d,"User reports headache",ft="DIAGNOSIS",prov=Provenance.USER_REPORTED); db.commit(); c=client(db,u); patch_provider(monkeypatch,fake_result("The record contains a user-reported headache.",["evidence_1"])); r=c.post(f"/api/patients/{p.id}/ask",json={"question":"headache"}); assert r.json()["evidence"][0]["provenance"]=="USER_REPORTED"; clean(); db.close()

def test_unsupported_claim_rejected(monkeypatch):
    S=make_db(); db,u,_,p,_=seed(S); d=doc(db,p,u,"lab.pdf"); fact(db,p,d,"Glucose 100 mg/dL"); db.commit(); c=client(db,u); patch_provider(monkeypatch,fake_result("The patient has diabetes.",["evidence_1"])); r=c.post(f"/api/patients/{p.id}/ask",json={"question":"headache"}); assert not r.json()["grounded"]; clean(); db.close()

def test_unauthorized_patient_access(monkeypatch):
    S=make_db(); db,u,v,p,q=seed(S); c=client(db,v); r=c.post(f"/api/patients/{p.id}/ask",json={"question":"What is in the record?"}); assert r.status_code==403; clean(); db.close()

def test_provider_failure_is_safe(monkeypatch):
    S=make_db(); db,u,_,p,_=seed(S); d=doc(db,p,u,"lab.pdf"); fact(db,p,d,"Glucose 100 mg/dL"); db.commit(); c=client(db,u); patch_provider(monkeypatch,error=AnswerProviderResponseError("boom")); r=c.post(f"/api/patients/{p.id}/ask",json={"question":"What was glucose?"}); assert r.status_code==503 and "boom" not in r.text; clean(); db.close()

def test_fake_provider_is_deterministic_and_never_gets_patient_id(monkeypatch):
    S=make_db(); db,u,_,p,_=seed(S); d=doc(db,p,u,"lab.pdf"); fact(db,p,d,"Glucose 100 mg/dL"); db.commit(); c=client(db,u); captures=[]; result=fake_result("Glucose was 100 mg/dL.",["evidence_1"]); patch_provider(monkeypatch,result,capture=captures); a=c.post(f"/api/patients/{p.id}/ask",json={"question":"What was glucose?"}).json(); b=c.post(f"/api/patients/{p.id}/ask",json={"question":"What was glucose?"}).json(); assert a==b; assert str(p.id) not in str(captures[0][1]); assert all("patient_id" not in x for x in captures[0][1]); clean(); db.close()
