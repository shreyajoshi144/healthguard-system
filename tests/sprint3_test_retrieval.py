from __future__ import annotations
from datetime import date
from uuid import uuid4
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from db.models import Base, DatePrecision, Document, DocumentStatus, HealthFact, Investigation, InvestigationResult, Medication, MedicationEvent, MedicationEventType, MedicationStatus, PatientProfile, Provenance, User
from backend.auth import get_current_user
from backend.main import app
from db.session import get_db


def make_db():
    e=create_engine("sqlite://",connect_args={"check_same_thread":False},poolclass=StaticPool)
    @event.listens_for(e,"connect")
    def fk(conn,_): conn.execute("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(e); return sessionmaker(bind=e)

def seed(S):
    db=S(); a=User(username=f"a-{uuid4().hex[:6]}",email=f"a-{uuid4().hex[:6]}@x.test",password_hash="x",email_verified=True); b=User(username=f"b-{uuid4().hex[:6]}",email=f"b-{uuid4().hex[:6]}@x.test",password_hash="x",email_verified=True); db.add_all([a,b]); db.flush(); p=PatientProfile(owner_user_id=a.id,first_name="A",last_name="P"); q=PatientProfile(owner_user_id=b.id,first_name="B",last_name="P"); db.add_all([p,q]); db.flush(); return db,a,b,p,q

def doc(db,p,u,name):
    d=Document(patient_id=p.id,uploaded_by=u.id,filename=name,content_type="application/pdf",file_size=10,storage_key=f"p/{p.id}/{uuid4()}",checksum=uuid4().hex+"0"*32,status=DocumentStatus.COMPLETED); db.add(d); db.flush(); return d

def fact(db,p,d,value,when=date(2025,1,2),ft="LAB_RESULT"):
    f=HealthFact(patient_id=p.id,document_id=d.id,fact_type=ft,value=value,normalized_value=value.lower(),temporal_date=when,temporal_precision=DatePrecision.EXACT,source_page=3,source_text=f"Exact evidence: {value}",confidence=.9,extraction_method="TEST",provenance=Provenance.DOCUMENT_VERIFIED); db.add(f); db.flush(); return f

def client(db,u):
    app.dependency_overrides[get_db]=lambda:db; app.dependency_overrides[get_current_user]=lambda:u; return TestClient(app)

def clean(): app.dependency_overrides.clear()

def test_medication_question():
    S=make_db(); db,u,_,p,_=seed(S); m=Medication(patient_id=p.id,medicine_name="Metformin",dose="500 mg",status=MedicationStatus.ACTIVE,provenance=Provenance.USER_REPORTED,created_by=u.id); db.add(m); db.commit(); from domain.retrieval import retrieve_patient_evidence; r=retrieve_patient_evidence(db,p.id,"What medication is Metformin?"); assert r and r[0]["source_type"]=="MEDICATION"; db.close()

def test_lab_question_preserves_exact_source():
    S=make_db(); db,u,_,p,_=seed(S); d=doc(db,p,u,"lab.pdf"); f=fact(db,p,d,"Hemoglobin 13.2 g/dL"); db.commit(); from domain.retrieval import retrieve_patient_evidence; r=retrieve_patient_evidence(db,p.id,"hemoglobin"); assert r[0]["fact_id"]==f.id and r[0]["source_text"]==f.source_text and r[0]["page"]==3; db.close()

def test_investigation_question():
    S=make_db(); db,u,_,p,_=seed(S); i=Investigation(patient_id=p.id,name="MRI brain",performed_date=date(2025,2,1),provenance=Provenance.USER_REPORTED,created_by=u.id); db.add(i); db.flush(); db.add(InvestigationResult(investigation_id=i.id,result_summary="No acute findings",result_date=date(2025,2,2),created_by=u.id)); db.commit(); from domain.retrieval import retrieve_patient_evidence; r=retrieve_patient_evidence(db,p.id,"MRI result"); assert r and any(x["source_type"]=="INVESTIGATION_RESULT" for x in r); db.close()

def test_date_specific_query_and_multiple_documents():
    S=make_db(); db,u,_,p,_=seed(S); d1=doc(db,p,u,"a.pdf"); d2=doc(db,p,u,"b.pdf"); fact(db,p,d1,"Glucose 100 mg/dL",date(2024,5,1)); fact(db,p,d2,"Glucose 110 mg/dL",date(2025,5,1)); db.commit(); from domain.retrieval import retrieve_patient_evidence; r=retrieve_patient_evidence(db,p.id,"glucose 2025"); assert len(r)>=2 and r[0]["date"].year==2025; r2=retrieve_patient_evidence(db,p.id,"glucose",date_from=date(2025,1,1),date_to=date(2025,12,31)); assert len(r2)==1; db.close()

def test_no_evidence_and_limit_and_determinism():
    S=make_db(); db,u,_,p,_=seed(S); d=doc(db,p,u,"lab.pdf"); fact(db,p,d,"Vitamin D 20 ng/mL"); fact(db,p,d,"Vitamin D 20 ng/mL"); db.commit(); from domain.retrieval import retrieve_patient_evidence; assert retrieve_patient_evidence(db,p.id,"kidney transplant") == []; a=retrieve_patient_evidence(db,p.id,"Vitamin D",limit=1); b=retrieve_patient_evidence(db,p.id,"Vitamin D",limit=1); assert len(a)==1 and a==b; db.close()

def test_contradictory_facts_both_retrievable():
    S=make_db(); db,u,_,p,_=seed(S); d1=doc(db,p,u,"a.pdf"); d2=doc(db,p,u,"b.pdf"); fact(db,p,d1,"Hemoglobin 13.2 g/dL"); fact(db,p,d2,"Hemoglobin 10.1 g/dL"); db.commit(); from domain.retrieval import retrieve_patient_evidence; r=retrieve_patient_evidence(db,p.id,"hemoglobin"); assert {x["value"] for x in r}>={"Hemoglobin 13.2 g/dL","Hemoglobin 10.1 g/dL"}; db.close()

def test_provenance_preserved():
    S=make_db(); db,u,_,p,_=seed(S); d=doc(db,p,u,"record.pdf"); f=fact(db,p,d,"Type 2 diabetes",ft="DIAGNOSIS"); db.commit(); from domain.retrieval import retrieve_patient_evidence; r=retrieve_patient_evidence(db,p.id,"diabetes"); assert r[0]["provenance"]=="DOCUMENT_VERIFIED"; db.close()

def test_cross_patient_isolation_service_and_endpoint():
    S=make_db(); db,u,v,p,q=seed(S); d=doc(db,q,v,"secret.pdf"); fact(db,q,d,"Secret glucose 999"); db.commit(); from domain.retrieval import retrieve_patient_evidence; assert retrieve_patient_evidence(db,p.id,"secret glucose")==[]; c=client(db,u); response=c.post(f"/api/patients/{q.id}/retrieve",json={"query":"secret"}); assert response.status_code==403; clean(); db.close()

def test_unauthorized_access_and_auth_before_retrieval():
    S=make_db(); db,u,v,p,q=seed(S); c=client(db,v); response=c.post(f"/api/patients/{p.id}/retrieve",json={"query":"anything"}); assert response.status_code==403; clean(); db.close()

def test_document_text_block_is_retrievable():
    S=make_db(); db,u,_,p,_=seed(S); d=doc(db,p,u,"consult.pdf"); from db.models import DocumentTextBlock; b=DocumentTextBlock(document_id=d.id,page_number=4,block_index=0,source_text="Patient reports migraine headaches",extraction_method="PDF_TEXT",confidence=.95); db.add(b); db.commit(); from domain.retrieval import retrieve_patient_evidence; r=retrieve_patient_evidence(db,p.id,"migraine"); assert r[0]["source_type"]=="DOCUMENT_TEXT_BLOCK" and r[0]["source_text"]==b.source_text and r[0]["document_id"]==d.id; db.close()

def test_fact_type_is_explicitly_source_scoped():
    S=make_db(); db,u,_,p,_=seed(S)
    d=doc(db,p,u,"mixed.pdf")
    lab=fact(db,p,d,"Glucose 100 mg/dL",ft="LAB_RESULT")
    med=Medication(patient_id=p.id,medicine_name="Metformin",dose="500 mg",status=MedicationStatus.ACTIVE,provenance=Provenance.USER_REPORTED,created_by=u.id)
    inv=Investigation(patient_id=p.id,name="MRI brain",performed_date=date(2025,2,1),provenance=Provenance.USER_REPORTED,created_by=u.id)
    db.add_all([med,inv]); db.flush()
    db.add(InvestigationResult(investigation_id=inv.id,result_summary="No acute findings",result_date=date(2025,2,2),created_by=u.id))
    db.commit()
    from domain.retrieval import retrieve_patient_evidence
    lab_results=retrieve_patient_evidence(db,p.id,"metformin glucose",fact_type="LAB_RESULT")
    assert lab_results and all(x["source_type"]=="HEALTH_FACT" for x in lab_results)
    assert {x["fact_id"] for x in lab_results}=={lab.id}
    medication_results=retrieve_patient_evidence(db,p.id,"metformin",fact_type="MEDICATION")
    assert medication_results and all(x["source_type"] in {"HEALTH_FACT","MEDICATION","MEDICATION_EVENT"} for x in medication_results)
    investigation_results=retrieve_patient_evidence(db,p.id,"MRI result",fact_type="INVESTIGATION")
    assert investigation_results and all(x["source_type"] in {"HEALTH_FACT","INVESTIGATION","INVESTIGATION_RESULT"} for x in investigation_results)
    db.close()
