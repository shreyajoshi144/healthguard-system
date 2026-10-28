from __future__ import annotations
import asyncio
from pathlib import Path
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.models import Base, User, PatientProfile, Document, DocumentStatus, ProcessingJob
from db.session import get_db
import storage.service as storage_service
from storage.service import StorageError, Storage, sanitize_filename, validate_upload
from jobs.queue import QueueUnavailable

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

def verified_user(db,email):
    u=User(username=email.split('@')[0],email=email,password_hash='x',email_verified=True); db.add(u); db.commit(); return u

def test_filename_cannot_escape_storage_root():
    assert sanitize_filename("../../private/record.pdf")=="record.pdf"
    s=Storage(); s.root=Path("/tmp/healthguard-test-root").resolve(); s.backend="local"
    key=s.key(uuid4(),uuid4(),"../../escape.pdf")
    assert ".." not in Path(key).parts
    assert (s.root/key).resolve().is_relative_to(s.root)

def test_upload_size_limit_is_streaming(monkeypatch,tmp_path):
    class AsyncUpload:
        def __init__(self): self.calls=[]
        async def read(self,n):
            self.calls.append(n)
            return b"x"*n if len(self.calls)==1 else b""
    monkeypatch.setattr(storage_service,"MAX_FILE_SIZE",1024)
    from api.healthguard import _stream_upload
    with pytest.raises(StorageError): asyncio.run(_stream_upload(AsyncUpload()))

def test_oversized_upload_rejected(monkeypatch):
    monkeypatch.setattr(storage_service,"MAX_FILE_SIZE",4)
    with pytest.raises(StorageError): validate_upload("record.pdf","application/pdf",b"%PDF-1.7")

def test_storage_delete_failure_is_not_silently_ignored(monkeypatch,db):
    from backend.main import app
    owner=verified_user(db,"deletefail@example.com")
    p=PatientProfile(owner_user_id=owner.id,first_name="A",last_name="B"); db.add(p); db.flush()
    d=Document(patient_id=p.id,uploaded_by=owner.id,filename="r.pdf",content_type="application/pdf",file_size=8,storage_key=f"patients/{p.id}/{uuid4()}/r.pdf",checksum="c"*64,status=DocumentStatus.QUEUED); db.add(d); db.commit()
    app.dependency_overrides[get_db]=lambda:db
    from backend.auth import create_access_token
    monkeypatch.setattr("api.healthguard.storage.delete",lambda key: (_ for _ in ()).throw(RuntimeError("storage down")))
    r=TestClient(app).delete(f"/api/documents/{d.id}",headers={"Authorization":f"Bearer {create_access_token(owner)}"})
    assert r.status_code==503
    db.refresh(d); assert d.status is DocumentStatus.DELETION_PENDING

def test_repeated_delete_is_safe(monkeypatch,db,tmp_path):
    from backend.main import app
    from backend.auth import create_access_token
    owner=verified_user(db,"repeat@example.com")
    p=PatientProfile(owner_user_id=owner.id,first_name="A",last_name="B"); db.add(p); db.flush()
    s=Storage(); s.backend="local"; s.root=tmp_path
    key=s.key(p.id,uuid4(),"record.pdf")
    d=Document(patient_id=p.id,uploaded_by=owner.id,filename="record.pdf",content_type="application/pdf",file_size=4,storage_key=key,checksum="d"*64,status=DocumentStatus.QUEUED); db.add(d); db.commit()
    monkeypatch.setattr("api.healthguard.storage",s)
    app.dependency_overrides[get_db]=lambda:db
    c=TestClient(app); h={"Authorization":f"Bearer {create_access_token(owner)}"}
    assert c.delete(f"/api/documents/{d.id}",headers=h).status_code==204
    assert c.delete(f"/api/documents/{d.id}",headers=h).status_code==204
    db.refresh(d); assert d.status is DocumentStatus.DELETED


def test_private_object_access_and_cross_user_denied(monkeypatch,db,tmp_path):
    from backend.main import app
    from backend.auth import create_access_token
    owner=verified_user(db,"private-owner@example.com"); outsider=verified_user(db,"private-outsider@example.com")
    p=PatientProfile(owner_user_id=owner.id,first_name="A",last_name="B"); db.add(p); db.flush()
    s=Storage(); s.backend="local"; s.root=tmp_path
    key=s.key(p.id,uuid4(),"private.pdf"); s.put(key,b"%PDF-private","application/pdf")
    d=Document(patient_id=p.id,uploaded_by=owner.id,filename="private.pdf",content_type="application/pdf",file_size=13,storage_key=key,checksum="e"*64,status=DocumentStatus.QUEUED); db.add(d); db.commit()
    monkeypatch.setattr("api.healthguard.storage",s)
    app.dependency_overrides[get_db]=lambda:db
    c=TestClient(app)
    assert c.get(f"/api/documents/{d.id}/content").status_code==401
    assert c.get(f"/api/documents/{d.id}/content",headers={"Authorization":f"Bearer {create_access_token(outsider)}"}).status_code==403
    assert c.get(f"/api/documents/{d.id}/content",headers={"Authorization":f"Bearer {create_access_token(owner)}"}).status_code==200


def test_duplicate_checksum_behavior(monkeypatch,db,tmp_path):
    from backend.main import app
    from backend.auth import create_access_token
    owner=verified_user(db,"duplicate@example.com")
    p=PatientProfile(owner_user_id=owner.id,first_name="A",last_name="B"); db.add(p); db.commit()
    s=Storage(); s.backend="local"; s.root=tmp_path
    monkeypatch.setattr("api.healthguard.storage",s)
    app.dependency_overrides[get_db]=lambda:db
    c=TestClient(app); h={"Authorization":f"Bearer {create_access_token(owner)}"}
    pdf=b"%PDF-1.7 duplicate"; files={"file":("record.pdf",pdf,"application/pdf")}
    r1=c.post(f"/api/patients/{p.id}/documents",headers=h,files=files)
    assert r1.status_code==201
    r2=c.post(f"/api/patients/{p.id}/documents",headers=h,files=files)
    assert r2.status_code==409
    assert db.query(Document).filter(Document.patient_id==p.id).count()==1


def test_missing_object_handled(db,tmp_path,monkeypatch):
    from backend.main import app
    from backend.auth import create_access_token
    owner=verified_user(db,"missing@example.com")
    p=PatientProfile(owner_user_id=owner.id,first_name="A",last_name="B"); db.add(p); db.flush()
    s=Storage(); s.backend="local"; s.root=tmp_path
    key=s.key(p.id,uuid4(),"missing.pdf")
    d=Document(patient_id=p.id,uploaded_by=owner.id,filename="missing.pdf",content_type="application/pdf",file_size=8,storage_key=key,checksum="f"*64,status=DocumentStatus.QUEUED); db.add(d); db.commit()
    monkeypatch.setattr("api.healthguard.storage",s)
    app.dependency_overrides[get_db]=lambda:db
    c=TestClient(app); h={"Authorization":f"Bearer {create_access_token(owner)}"}
    assert c.get(f"/api/documents/{d.id}/content",headers=h).status_code==404
    assert c.delete(f"/api/documents/{d.id}",headers=h).status_code==204
    db.refresh(d); assert d.status is DocumentStatus.DELETED


def test_patient_deletion_storage_failure_is_recoverable(monkeypatch, db, tmp_path):
    from backend.main import app
    from backend.auth import create_access_token
    owner=verified_user(db,"patient-delete@example.com")
    p=PatientProfile(owner_user_id=owner.id,first_name="A",last_name="B"); db.add(p); db.flush()
    d=Document(patient_id=p.id,uploaded_by=owner.id,filename="record.pdf",content_type="application/pdf",file_size=8,storage_key=f"patients/{p.id}/{uuid4()}/r.pdf",checksum="a"*64,status=DocumentStatus.QUEUED); db.add(d); db.commit()
    app.dependency_overrides[get_db]=lambda:db
    monkeypatch.setattr("api.healthguard.storage.delete",lambda key: (_ for _ in ()).throw(RuntimeError("storage down")))
    h={"Authorization":f"Bearer {create_access_token(owner)}"}
    c=TestClient(app)
    r=c.delete(f"/api/patients/{p.id}",headers=h)
    assert r.status_code==503
    db.refresh(p)
    assert p.deletion_pending is True
    assert db.get(Document,d.id) is not None


def test_patient_deletion_retry_is_idempotent(monkeypatch, db, tmp_path):
    from backend.main import app
    from backend.auth import create_access_token
    owner=verified_user(db,"patient-retry@example.com")
    p=PatientProfile(owner_user_id=owner.id,first_name="A",last_name="B"); db.add(p); db.flush()
    s=Storage(); s.backend="local"; s.root=Path(tmp_path)
    d=Document(patient_id=p.id,uploaded_by=owner.id,filename="record.pdf",content_type="application/pdf",file_size=8,storage_key=s.key(p.id,uuid4(),"record.pdf"),checksum="b"*64,status=DocumentStatus.QUEUED); db.add(d); db.commit()
    app.dependency_overrides[get_db]=lambda:db
    monkeypatch.setattr("api.healthguard.storage",s)
    h={"Authorization":f"Bearer {create_access_token(owner)}"}
    c=TestClient(app)
    original=s.delete
    calls={"n":0}
    def fail_once(key):
        calls["n"]+=1
        if calls["n"]==1: raise RuntimeError("temporary")
        return original(key)
    monkeypatch.setattr(s,"delete",fail_once)
    assert c.delete(f"/api/patients/{p.id}",headers=h).status_code==503
    assert c.delete(f"/api/patients/{p.id}",headers=h).status_code==204
    assert db.get(PatientProfile,p.id) is None


def test_account_deletion_storage_failure_is_recoverable(monkeypatch, db):
    from backend.main import app
    from backend.auth import create_access_token
    owner=verified_user(db,"account-delete@example.com")
    p=PatientProfile(owner_user_id=owner.id,first_name="A",last_name="B"); db.add(p); db.flush()
    d=Document(patient_id=p.id,uploaded_by=owner.id,filename="record.pdf",content_type="application/pdf",file_size=8,storage_key=f"patients/{p.id}/{uuid4()}/r.pdf",checksum="c"*64,status=DocumentStatus.QUEUED); db.add(d); db.commit()
    app.dependency_overrides[get_db]=lambda:db
    monkeypatch.setattr("api.healthguard.storage.delete",lambda key: (_ for _ in ()).throw(RuntimeError("storage down")))
    h={"Authorization":f"Bearer {create_access_token(owner)}"}
    c=TestClient(app)
    r=c.delete("/api/account",headers=h)
    assert r.status_code==503
    db.refresh(owner)
    assert owner.deletion_pending is True
    assert db.get(PatientProfile,p.id) is not None


def test_upload_job_creation_failure_is_recoverable(monkeypatch, db, tmp_path):
    from backend.main import app
    from backend.auth import create_access_token
    from jobs.queue import QueueUnavailable
    owner=verified_user(db,"job-failure@example.com")
    p=PatientProfile(owner_user_id=owner.id,first_name="A",last_name="B"); db.add(p); db.commit()
    s=Storage(); s.backend="local"; s.root=Path(tmp_path)
    monkeypatch.setattr("api.healthguard.storage",s)
    monkeypatch.setattr("api.healthguard.enqueue_document",lambda job_id: (_ for _ in ()).throw(QueueUnavailable("queue down")))
    app.dependency_overrides[get_db]=lambda:db
    c=TestClient(app); h={"Authorization":f"Bearer {create_access_token(owner)}"}
    r=c.post(f"/api/patients/{p.id}/documents",headers=h,files={"file":("record.pdf",b"%PDF-1.7 recovery","application/pdf")})
    assert r.status_code==503
    doc=db.query(Document).filter(Document.patient_id==p.id).one()
    job=db.query(__import__('db.models',fromlist=['ProcessingJob']).ProcessingJob).filter_by(document_id=doc.id).one()
    assert doc.status is DocumentStatus.QUEUED
    assert job.status is DocumentStatus.QUEUED
    assert s.root.joinpath(doc.storage_key).exists()


def test_partial_patient_deletion_object_a_succeeds_object_b_fails_then_recovers(monkeypatch, db, tmp_path):
    from backend.main import app
    from backend.auth import create_access_token
    owner=verified_user(db,"partial-patient@example.com")
    p=PatientProfile(owner_user_id=owner.id,first_name="A",last_name="B"); db.add(p); db.flush()
    s=Storage(); s.backend="local"; s.root=Path(tmp_path)
    keys=[]
    for name,checksum in (("a.pdf","1"*64),("b.pdf","2"*64)):
        key=s.key(p.id,uuid4(),name); s.put(key,b"%PDF-1.7 "+name.encode(),"application/pdf")
        d=Document(patient_id=p.id,uploaded_by=owner.id,filename=name,content_type="application/pdf",file_size=12,storage_key=key,checksum=checksum,status=DocumentStatus.QUEUED); db.add(d); keys.append(key)
    db.commit(); app.dependency_overrides[get_db]=lambda:db; monkeypatch.setattr("api.healthguard.storage",s)
    original=s.delete; calls=[]
    def fail_on_b(key):
        calls.append(key)
        if key==keys[1]: raise RuntimeError("temporary storage failure")
        return original(key)
    monkeypatch.setattr(s,"delete",fail_on_b)
    h={"Authorization":f"Bearer {create_access_token(owner)}"}; c=TestClient(app)
    r=c.delete(f"/api/patients/{p.id}",headers=h); assert r.status_code==503
    assert not Path(s.root/keys[0]).exists()
    assert Path(s.root/keys[1]).exists()
    db.refresh(p); assert p.deletion_pending is True
    monkeypatch.setattr(s,"delete",original)
    assert c.delete(f"/api/patients/{p.id}",headers=h).status_code==204
    assert db.get(PatientProfile,p.id) is None
    assert not Path(s.root/keys[1]).exists()


def test_partial_account_deletion_object_a_succeeds_object_b_fails_then_recovers(monkeypatch, db, tmp_path):
    from backend.main import app
    from backend.auth import create_access_token
    owner=verified_user(db,"partial-account@example.com")
    p=PatientProfile(owner_user_id=owner.id,first_name="A",last_name="B"); db.add(p); db.flush()
    s=Storage(); s.backend="local"; s.root=Path(tmp_path); keys=[]
    for name,checksum in (("a.pdf","3"*64),("b.pdf","4"*64)):
        key=s.key(p.id,uuid4(),name); s.put(key,b"%PDF-1.7 "+name.encode(),"application/pdf")
        db.add(Document(patient_id=p.id,uploaded_by=owner.id,filename=name,content_type="application/pdf",file_size=12,storage_key=key,checksum=checksum,status=DocumentStatus.QUEUED)); keys.append(key)
    db.commit(); app.dependency_overrides[get_db]=lambda:db; monkeypatch.setattr("api.healthguard.storage",s)
    original=s.delete
    def fail_on_b(key):
        if key==keys[1]: raise RuntimeError("temporary storage failure")
        return original(key)
    monkeypatch.setattr(s,"delete",fail_on_b)
    h={"Authorization":f"Bearer {create_access_token(owner)}"}; c=TestClient(app)
    r=c.delete("/api/account",headers=h); assert r.status_code==503
    db.refresh(owner); assert owner.deletion_pending is True
    assert not Path(s.root/keys[0]).exists() and Path(s.root/keys[1]).exists()
    monkeypatch.setattr(s,"delete",original)
    assert c.delete("/api/account",headers=h).status_code==204
    assert db.get(User,owner.id) is None and not Path(s.root/keys[1]).exists()


def test_failed_upload_recovery_succeeds_on_same_upload(monkeypatch, db, tmp_path):
    from backend.main import app
    from backend.auth import create_access_token
    owner=verified_user(db,"upload-recovery@example.com")
    p=PatientProfile(owner_user_id=owner.id,first_name="A",last_name="B"); db.add(p); db.commit()
    s=Storage(); s.backend="local"; s.root=Path(tmp_path); monkeypatch.setattr("api.healthguard.storage",s); app.dependency_overrides[get_db]=lambda:db
    monkeypatch.setattr("api.healthguard.enqueue_document",lambda job_id: (_ for _ in ()).throw(QueueUnavailable("queue down")))
    c=TestClient(app); h={"Authorization":f"Bearer {create_access_token(owner)}"}; files={"file":("record.pdf",b"%PDF-1.7 recover","application/pdf")}
    r=c.post(f"/api/patients/{p.id}/documents",headers=h,files=files); assert r.status_code==503
    doc=db.query(Document).filter_by(patient_id=p.id).one(); assert doc.status is DocumentStatus.QUEUED
    monkeypatch.setattr("api.healthguard.enqueue_document",lambda job_id:"REDIS")
    r2=c.post(f"/api/documents/{doc.id}/retry-processing",headers=h); assert r2.status_code==200
    db.refresh(doc); assert doc.status is DocumentStatus.QUEUED
    job=db.query(ProcessingJob).filter_by(document_id=doc.id).one(); assert job.status is DocumentStatus.QUEUED
    assert Path(s.root/doc.storage_key).exists()


def test_failed_queue_job_has_explicit_retry_path(monkeypatch, db, tmp_path):
    from backend.main import app
    from backend.auth import create_access_token
    owner=verified_user(db,"queue-retry@example.com")
    p=PatientProfile(owner_user_id=owner.id,first_name="A",last_name="B"); db.add(p); db.commit()
    s=Storage(); s.backend="local"; s.root=Path(tmp_path); monkeypatch.setattr("api.healthguard.storage",s); app.dependency_overrides[get_db]=lambda:db
    monkeypatch.setattr("api.healthguard.enqueue_document",lambda job_id: (_ for _ in ()).throw(QueueUnavailable("queue down")))
    c=TestClient(app); h={"Authorization":f"Bearer {create_access_token(owner)}"}; files={"file":("record.pdf",b"%PDF-1.7 queue","application/pdf")}
    assert c.post(f"/api/patients/{p.id}/documents",headers=h,files=files).status_code==503
    doc=db.query(Document).filter_by(patient_id=p.id).one()
    monkeypatch.setattr("api.healthguard.enqueue_document",lambda job_id:"REDIS")
    r=c.post(f"/api/documents/{doc.id}/retry-processing",headers=h); assert r.status_code==200
    db.refresh(doc); assert doc.status is DocumentStatus.QUEUED
    assert r.json()["status"]=="QUEUED"
