from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from db.models import Base, Document, DocumentStatus, PatientProfile, ProcessingJob, User, AccountStatus


def _session(tmp_path):
    engine=create_engine(f"sqlite:///{tmp_path/'reliability.db'}", future=True, connect_args={"check_same_thread":False})
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)()


def test_production_db_pool_configuration_contract():
    text=Path("db/session.py").read_text()
    assert "pool_size" in text and "max_overflow" in text and "pool_timeout" in text and "pool_recycle" in text
    assert "pool_pre_ping" in text and "True" in text
    assert "connect_timeout" in text


def test_stuck_processing_job_is_requeued(tmp_path, monkeypatch):
    from jobs import worker
    engine, db=_session(tmp_path)
    user=User(username="owner",email="owner@example.com",password_hash="x",status=AccountStatus.ACTIVE,email_verified=True)
    db.add(user); db.flush()
    patient=PatientProfile(owner_user_id=user.id,first_name="A",last_name="B"); db.add(patient); db.flush()
    doc=Document(patient_id=patient.id,uploaded_by=user.id,filename="a.pdf",content_type="application/pdf",file_size=8,storage_key=f"patients/{patient.id}/documents/{uuid4()}/a.pdf",checksum="a"*64,status=DocumentStatus.PROCESSING); db.add(doc); db.flush()
    job=ProcessingJob(document_id=doc.id,status=DocumentStatus.PROCESSING,attempts=1,lease_expires_at=datetime.now(timezone.utc)-timedelta(seconds=1)); db.add(job); db.commit()
    monkeypatch.setattr(worker,"MAX_ATTEMPTS",3)
    monkeypatch.setattr(worker,"SessionLocal",lambda:db)
    assert worker.recover_stuck_jobs()==1
    db.expire_all(); refreshed=db.get(ProcessingJob,job.id); assert refreshed.status is DocumentStatus.QUEUED
    assert db.get(Document,doc.id).status is DocumentStatus.QUEUED
    db.close(); engine.dispose()


def test_max_attempts_terminal_failure(tmp_path, monkeypatch):
    from jobs import worker
    engine, db=_session(tmp_path)
    user=User(username="owner2",email="owner2@example.com",password_hash="x",status=AccountStatus.ACTIVE,email_verified=True)
    db.add(user); db.flush(); patient=PatientProfile(owner_user_id=user.id,first_name="A",last_name="B"); db.add(patient); db.flush()
    doc=Document(patient_id=patient.id,uploaded_by=user.id,filename="a.pdf",content_type="application/pdf",file_size=8,storage_key=f"patients/{patient.id}/documents/{uuid4()}/a.pdf",checksum="b"*64,status=DocumentStatus.PROCESSING); db.add(doc); db.flush()
    job=ProcessingJob(document_id=doc.id,status=DocumentStatus.PROCESSING,attempts=3,lease_expires_at=datetime.now(timezone.utc)-timedelta(seconds=1)); db.add(job); db.commit()
    monkeypatch.setattr(worker,"MAX_ATTEMPTS",3)
    monkeypatch.setattr(worker,"SessionLocal",lambda:db)
    assert worker.recover_stuck_jobs()==0
    db.expire_all(); assert db.get(ProcessingJob,job.id).status is DocumentStatus.FAILED
    assert db.get(Document,doc.id).status is DocumentStatus.FAILED
    db.close(); engine.dispose()


def test_queue_message_recovery_is_idempotent(monkeypatch):
    from jobs import queue
    class FakeRedis:
        def __init__(self): self.processing=[]; self.queue=[]
        def lrange(self,*args): return list(self.processing)
        def eval(self,script,numkeys,*args):
            raw=args[2]
            if "LREM" in script and "RPUSH" in script:
                if raw not in self.processing: return 0
                self.processing.remove(raw); self.queue.append(raw); return 1
            raise AssertionError("unexpected redis script")
    fake=FakeRedis(); raw=json.dumps({"job_id":str(uuid4()),"claimed_at":0}); fake.processing=[raw]
    monkeypatch.setattr(queue,"_client",lambda:fake); monkeypatch.setattr(queue,"VISIBILITY_SECONDS",1)
    assert queue.recover_stale_queue_items()==1
    assert queue.recover_stale_queue_items()==0
    assert len(fake.queue)==1


def test_request_id_and_safe_logging():
    from backend.main import app
    response=TestClient(app).get("/api/health")
    assert response.status_code==200
    assert response.headers.get("X-Request-ID")


def test_sensitive_log_messages_are_redacted(caplog):
    from utils.logger import _RedactionFilter
    record=logging.LogRecord("test",logging.ERROR,"",0,"Authorization: Bearer secret-token",(),None)
    assert _RedactionFilter().filter(record)
    assert record.msg == "[REDACTED SENSITIVE LOG MESSAGE]"


def test_readiness_distinguishes_dependency_failure(monkeypatch):
    from backend.main import readiness
    class BadEngine:
        def connect(self): raise RuntimeError("db down")
    monkeypatch.setattr("db.session.engine",BadEngine())
    result=readiness()
    assert result.status_code in {200,503}
    body=result.body.decode()
    assert '"database":false' in body
    assert "db down" not in body


def test_redis_claim_is_atomic_and_stamps_visibility(monkeypatch):
    from jobs import queue
    class FakeRedis:
        def __init__(self): self.calls=[]
        def eval(self, script, numkeys, *args):
            self.calls.append((script,numkeys,args)); return '{"job_id":"job-1","claimed_at":123.0}'
        def close(self): pass
    fake=FakeRedis(); monkeypatch.setattr(queue,"_client",lambda:fake)
    claimed=queue.claim_document_message(timeout=5)
    assert claimed and 'claimed_at' in claimed
    assert len(fake.calls)==1
    script,numkeys,args=fake.calls[0]
    assert numkeys==3 and args[0]==queue.QUEUE_KEY and args[1]==queue.PROCESSING_KEY and args[2]==queue.QUARANTINE_KEY
    assert "RPOP" in script and "LPUSH" in script and "claimed_at" in script


def test_redis_client_has_bounded_timeouts_and_health_checks(monkeypatch):
    from jobs import queue
    captured={}
    class FakeRedisModule:
        @staticmethod
        def from_url(url, **kwargs): captured.update(kwargs); return object()
    monkeypatch.setattr(queue,"redis",FakeRedisModule)
    monkeypatch.setenv("REDIS_URL","redis://localhost:6379/0")
    queue._client()
    assert captured["socket_connect_timeout"] > 0
    assert captured["socket_timeout"] > 0
    assert captured["retry_on_timeout"] is True
    assert captured["health_check_interval"] > 0


def test_worker_shutdown_is_graceful(monkeypatch, caplog):
    from jobs import worker
    worker.SHUTDOWN_EVENT.clear()
    monkeypatch.setattr(worker, "run_once", lambda timeout=5: worker.SHUTDOWN_EVENT.set())
    with caplog.at_level(logging.INFO):
        worker.run_forever()
    assert worker.SHUTDOWN_EVENT.is_set()
    assert any("stopped gracefully" in r.getMessage() for r in caplog.records)
    worker.SHUTDOWN_EVENT.clear()


def test_s3_client_uses_bounded_timeouts_and_standard_retries(monkeypatch):
    import storage.service as service
    captured={}
    class FakeBoto:
        @staticmethod
        def client(*args, **kwargs): captured.update(kwargs); return object()
    class FakeConfig:
        def __init__(self, **kwargs): self.kwargs=kwargs; captured["config"] = self
    monkeypatch.setattr(service,"boto3",FakeBoto)
    monkeypatch.setattr(service,"BotoConfig",FakeConfig)
    monkeypatch.setenv("STORAGE_BACKEND","s3")
    monkeypatch.setenv("STORAGE_BUCKET","healthguard")
    service.Storage()._s3()
    cfg=captured["config"].kwargs
    assert cfg["connect_timeout"] > 0
    assert cfg["read_timeout"] > 0
    assert cfg["retries"]["max_attempts"] >= 1
    assert cfg["retries"]["mode"] == "standard"


def test_worker_survives_redis_outage_and_resumes(monkeypatch, caplog):
    from jobs import worker
    state={"outage":True,"recovered":False,"processed":False}
    monkeypatch.setattr(worker, "recover_stuck_jobs", lambda: 0)
    def recover_stale():
        if state["outage"]: raise worker.QueueUnavailable("redis down")
        state["recovered"]=True
        return 0
    def claim(*, timeout=1):
        assert state["recovered"]
        return json.dumps({"job_id":"job-1","claimed_at":time.time()})
    monkeypatch.setattr(worker, "recover_stale_queue_items", recover_stale)
    monkeypatch.setattr(worker, "_enqueue_due_jobs", lambda: 0)
    monkeypatch.setattr(worker, "SHUTDOWN_EVENT", __import__("threading").Event())
    monkeypatch.setattr("jobs.queue.claim_document_message", claim)
    monkeypatch.setattr(worker, "process_job", lambda job_id: state.__setitem__("processed", True) or "READY")
    monkeypatch.setattr(worker, "acknowledge_document", lambda raw: None)
    with caplog.at_level(logging.WARNING):
        assert worker.run_once() is False
        assert not state["processed"]
        state["outage"]=False
        assert worker.run_once() is True
    assert state["processed"] is True
    assert any("dependency=redis" in r.getMessage() for r in caplog.records)


def test_stale_recovery_uses_one_atomic_redis_script(monkeypatch):
    from jobs import queue
    class FakeRedis:
        def __init__(self): self.processing=[]; self.queue=[]; self.calls=[]
        def lrange(self,*args): return list(self.processing)
        def eval(self,script,numkeys,*args):
            self.calls.append((script,numkeys,args))
            raw=args[2]
            if "LREM" in script and "RPUSH" in script:
                if raw not in self.processing: return 0
                self.processing.remove(raw)
                payload=json.loads(raw); payload["recovered_at"]=float(args[3])
                self.queue.append(json.dumps(payload)); return 1
            return 0
        def close(self): pass
    fake=FakeRedis(); raw=json.dumps({"job_id":"job-atomic","claimed_at":0}); fake.processing=[raw]
    monkeypatch.setattr(queue,"_client",lambda:fake); monkeypatch.setattr(queue,"VISIBILITY_SECONDS",1)
    assert queue.recover_stale_queue_items()==1
    assert len(fake.processing)==0 and len(fake.queue)==1
    script,numkeys,args=fake.calls[0]
    assert numkeys==2
    assert "LREM" in script and "RPUSH" in script
    assert script.index("LREM") < script.index("RPUSH")


def test_concurrent_enqueue_is_idempotent_and_retry_is_legitimate(monkeypatch):
    from jobs import queue
    import threading
    class FakeRedis:
        def __init__(self): self.keys=set(); self.queue=[]; self.processing=[]; self.lock=threading.Lock()
        def eval(self,script,numkeys,*args):
            with self.lock:
                if "SET" in script and "RPUSH" in script and "NX" in script:
                    key=args[1]
                    if key in self.keys: return 0
                    self.keys.add(key); self.queue.append(args[2]); return 1
                if "LREM" in script and "DEL" in script:
                    raw=args[2]
                    try: self.processing.remove(raw); removed=1
                    except ValueError: removed=0
                    if removed: self.keys.discard(args[1])
                    return removed
                raise AssertionError("unexpected redis script")
        def close(self): pass
    fake=FakeRedis(); monkeypatch.setenv("REDIS_URL","redis://fake/0"); monkeypatch.setenv("APP_ENV","production")
    monkeypatch.setattr(queue,"redis",object())
    monkeypatch.setattr(queue,"_client",lambda:fake)
    results=[]
    threads=[threading.Thread(target=lambda: results.append(queue.enqueue_document("job-concurrent"))) for _ in range(20)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert results.count("REDIS")==1
    assert results.count("REDIS_ALREADY_QUEUED")==19
    assert len(fake.queue)==1

    raw=fake.queue.pop(); fake.processing.append(raw)
    queue.acknowledge_document(raw)
    assert not fake.keys
    assert queue.enqueue_document("job-concurrent")=="REDIS"
    assert len(fake.queue)==1


def test_run_forever_keeps_process_alive_across_redis_outage(monkeypatch):
    from jobs import worker
    import threading
    event=threading.Event(); calls=[]
    monkeypatch.setattr(worker,"SHUTDOWN_EVENT",event)
    def one_iteration(timeout=1):
        calls.append(len(calls)+1)
        if len(calls)==1: raise worker.QueueUnavailable("redis down")
        event.set()
        return True
    sleeps=[]
    monkeypatch.setattr(worker,"run_once",one_iteration)
    monkeypatch.setattr(worker.time,"sleep",lambda seconds: sleeps.append(seconds))
    worker.run_forever()
    assert calls==[1,2]
    assert sleeps and all(0 < value <= 10 for value in sleeps)


def test_malformed_queue_payload_is_quarantined_atomically(monkeypatch, caplog):
    from jobs import queue
    class FakeRedis:
        def __init__(self): self.queue=['{not-json']; self.processing=[]; self.quarantine=[]; self.calls=[]
        def eval(self, script, numkeys, *args):
            self.calls.append((script,numkeys,args))
            raw=self.queue.pop()
            if "pcall(cjson.decode" in script and "__POISON__" in script:
                self.quarantine.append(raw)
                return '__POISON__'
            raise AssertionError("unexpected claim script")
        def close(self): pass
    fake=FakeRedis(); monkeypatch.setattr(queue,"_client",lambda:fake)
    with caplog.at_level(logging.WARNING):
        assert queue.claim_document_message() is None
    assert fake.queue == []
    assert fake.quarantine == ['{not-json']
    script, numkeys, args = fake.calls[0]
    assert numkeys == 3
    assert args[0] == queue.QUEUE_KEY and args[1] == queue.PROCESSING_KEY and args[2] == queue.QUARANTINE_KEY
    assert "RPOP" in script and "LPUSH" in script and "pcall(cjson.decode" in script
    assert not any('{not-json' in r.getMessage() for r in caplog.records)


def test_malformed_processing_message_quarantines_and_releases_dedup(monkeypatch):
    from jobs import queue
    job_id=str(uuid4())
    raw='{"job_id":"'+job_id+'", "claimed_at":'
    class FakeRedis:
        def __init__(self):
            self.processing=[raw]; self.queue=[]; self.quarantine=[]; self.keys={queue.QUEUE_DEDUP_PREFIX+job_id}; self.calls=[]
        def lrange(self,*args): return list(self.processing)
        def eval(self, script, numkeys, *args):
            self.calls.append((script,numkeys,args))
            if "pcall(cjson.decode" in script and "return -1" in script:
                return -1
            if "LREM" in script and "DEL" in script:
                raw_arg=args[2]
                if raw_arg in self.processing:
                    self.processing.remove(raw_arg); self.quarantine.append(raw_arg)
                    dedup=args[3]
                    if dedup: self.keys.discard(dedup)
                    return 1
                return 0
            raise AssertionError("unexpected poison script")
            return 0
        def close(self): pass
    fake=FakeRedis(); monkeypatch.setattr(queue,"_client",lambda:fake)
    assert queue.recover_stale_queue_items()==0
    assert fake.processing == []
    assert fake.quarantine == [raw]
    assert queue.QUEUE_DEDUP_PREFIX+job_id not in fake.keys
    assert any("DEL" in call[0] and "LREM" in call[0] for call in fake.calls)


def test_valid_queue_payload_still_claims_normally(monkeypatch):
    from jobs import queue
    raw=json.dumps({"job_id":str(uuid4()),"enqueued_at":1.0})
    class FakeRedis:
        def __init__(self): self.queue=[raw]; self.processing=[]
        def eval(self, script, numkeys, *args):
            item=self.queue.pop()
            payload=json.loads(item); payload['claimed_at']=123.0
            claimed=json.dumps(payload,separators=(',',':'))
            self.processing.append(claimed)
            return claimed
        def close(self): pass
    fake=FakeRedis(); monkeypatch.setattr(queue,"_client",lambda:fake)
    claimed=queue.claim_document_message()
    assert claimed and json.loads(claimed)["job_id"]
    assert json.loads(claimed)["claimed_at"] == 123.0
    assert len(fake.processing)==1


def test_malformed_processing_without_safe_job_id_is_quarantined_without_dedup_change(monkeypatch):
    from jobs import queue
    raw='{"job_id":"not-a-uuid",'
    dedup=queue.QUEUE_DEDUP_PREFIX+'not-a-uuid'
    class FakeRedis:
        def __init__(self): self.processing=[raw]; self.quarantine=[]; self.keys={dedup}; self.calls=[]
        def lrange(self,*args): return list(self.processing)
        def eval(self, script, numkeys, *args):
            self.calls.append((script,numkeys,args))
            if "pcall(cjson.decode" in script and "return -1" in script: return -1
            if "LREM" in script and "DEL" in script:
                raw_arg=args[2]
                self.processing.remove(raw_arg); self.quarantine.append(raw_arg); return 1
            raise AssertionError("unexpected poison script")
        def close(self): pass
    fake=FakeRedis(); monkeypatch.setattr(queue,"_client",lambda:fake)
    assert queue.recover_stale_queue_items()==0
    assert fake.processing==[] and fake.quarantine==[raw]
    assert dedup in fake.keys
