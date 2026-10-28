"""Real PostgreSQL (pgserver, a genuine Postgres binary): migrations up/down/up, zero schema drift vs the ORM models,
data preservation across the enum->varchar alignment, reminder idempotency constraints and row-level claim exclusivity."""
from __future__ import annotations
import os, subprocess, sys, threading, uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import pytest
pgserver = pytest.importorskip("pgserver", reason="install pgserver (requirements-dev.txt) to run real-PostgreSQL tests")
from sqlalchemy import create_engine, text, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[2]

@pytest.fixture(scope="module")
def pg(tmp_path_factory):
    data = tmp_path_factory.mktemp("pg")
    srv = pgserver.get_server(data, cleanup_mode="stop")
    url = f"postgresql+psycopg://postgres:@/postgres?host={data}"
    yield url
    srv.cleanup()

def alembic(url, *args):
    return subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, env={**os.environ, "DATABASE_URL": url, "PYTHONPATH": str(ROOT)}, capture_output=True, text=True)

def reset(url):
    e = create_engine(url)
    with e.begin() as c: c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
    e.dispose()

def test_upgrade_check_downgrade_roundtrip_has_no_drift(pg):
    reset(pg)
    r = alembic(pg, "upgrade", "head"); assert r.returncode == 0, r.stderr[-800:]
    chk = alembic(pg, "check"); assert chk.returncode == 0 and "No new upgrade operations detected" in (chk.stdout + chk.stderr), chk.stderr[-800:]
    assert alembic(pg, "downgrade", "0013_p0_admin_ops").returncode == 0
    assert alembic(pg, "upgrade", "head").returncode == 0
    assert "No new upgrade operations detected" in (lambda x: x.stdout + x.stderr)(alembic(pg, "check"))
    e = create_engine(pg)
    with e.connect() as c:
        gone = ("documentstatus_document", "documentstatus_job", "provenance_health_event", "provenance_investigation", "provenance_medication", "provenance_medication_event")
        assert c.execute(text("select count(*) from pg_type where typname = any(:n)"), {"n": list(gone)}).scalar() == 0      # replaced enum types were dropped
        head = c.execute(text("select version_num from alembic_version")).scalar()
    assert head == "0018_consents"

def test_enum_alignment_preserves_existing_rows(pg):
    reset(pg); assert alembic(pg, "upgrade", "0014_health_action_confirmation").returncode == 0
    e = create_engine(pg); u, p, d = (str(uuid.uuid4()) for _ in range(3))
    with e.begin() as c:
        c.execute(text("insert into users(id,username,email,password_hash,email_verified,status,session_version,created_at,updated_at,deletion_pending) values(:u,'m','m@example.com','x',true,'ACTIVE',1,now(),now(),false)"), {"u": u})
        c.execute(text("insert into patient_profiles(id,owner_user_id,first_name,last_name,deletion_pending,created_at,updated_at) values(:p,:u,'A','B',false,now(),now())"), {"p": p, "u": u})
        c.execute(text("insert into documents(id,patient_id,uploaded_by,filename,content_type,file_size,storage_key,checksum,document_type,status,created_at,updated_at) values(:d,:p,:u,'f.pdf','application/pdf',1,'k',:h,'UNKNOWN','DELETION_PENDING',now(),now())"), {"d": d, "p": p, "u": u, "h": "a" * 64})
        c.execute(text("insert into medications(id,patient_id,medicine_name,status,provenance,created_by,created_at,updated_at) values(:i,:p,'M','ACTIVE','SYSTEM_DERIVED',:u,now(),now())"), {"i": str(uuid.uuid4()), "p": p, "u": u})
    assert alembic(pg, "upgrade", "head").returncode == 0
    with e.connect() as c:
        assert c.execute(text("select status from documents")).scalars().all() == ["DELETION_PENDING"]
        assert c.execute(text("select provenance from medications")).scalars().all() == ["SYSTEM_DERIVED"]

def test_reminder_idempotency_and_exclusive_claims_on_real_postgres(pg):
    reset(pg); assert alembic(pg, "upgrade", "head").returncode == 0
    from db.models import User, PatientProfile, HealthAction, HealthActionStatus, HealthActionSource, Provenance, ReminderDelivery
    from domain import reminders as R
    S = sessionmaker(bind=create_engine(pg, pool_size=5)); db = S()
    u = User(username="pgu", email="pgu@example.com", password_hash="x", email_verified=True); db.add(u); db.flush()
    p = PatientProfile(owner_user_id=u.id, first_name="A", last_name="B"); db.add(p); db.flush()
    due = date.today() + timedelta(days=3)
    act = HealthAction(patient_id=p.id, title="t", action_type="FOLLOW_UP", source=HealthActionSource.USER_CREATED, provenance=Provenance.USER_REPORTED, status=HealthActionStatus.SCHEDULED, due_date=due); db.add(act); db.flush()
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    db.add(ReminderDelivery(user_id=u.id, patient_id=p.id, action_id=act.id, lead_days=1, due_date=due, scheduled_for=past, status="PENDING")); db.commit()
    with pytest.raises(IntegrityError):                                                                   # database-level idempotency
        db.add(ReminderDelivery(user_id=u.id, patient_id=p.id, action_id=act.id, lead_days=1, due_date=due, scheduled_for=past, status="PENDING")); db.commit()
    db.rollback()
    ids = []
    for i in range(40):
        d = ReminderDelivery(user_id=u.id, patient_id=p.id, action_id=act.id, lead_days=2, due_date=due + timedelta(days=i), scheduled_for=past, status="PENDING"); db.add(d); db.flush(); ids.append(d.id)
    db.commit(); db.close()
    results, barrier = [[], [], []], threading.Barrier(3)
    def worker(n):
        s = S(); barrier.wait(); results[n] = R.claim_due(s, datetime.now(timezone.utc), limit=100); s.close()
    th = [threading.Thread(target=worker, args=(n,)) for n in range(3)]; [t.start() for t in th]; [t.join() for t in th]
    claimed = [x for r in results for x in r]
    assert len(claimed) == len(set(claimed)) == 41 and set(claimed) >= set(ids)                             # three concurrent workers, no delivery claimed twice


def test_purge_migration_removes_only_content_of_deleted_documents_on_real_postgres(pg):
    reset(pg); assert alembic(pg, "upgrade", "0016_email_reminders").returncode == 0
    e = create_engine(pg); u, p = str(uuid.uuid4()), str(uuid.uuid4())
    docs = {s: str(uuid.uuid4()) for s in ("DELETED", "DELETION_PENDING", "COMPLETED")}
    with e.begin() as c:
        c.execute(text("insert into users(id,username,email,password_hash,email_verified,status,session_version,created_at,updated_at,deletion_pending) values(:u,'m','m@example.com','x',true,'ACTIVE',1,now(),now(),false)"), {"u": u})
        c.execute(text("insert into patient_profiles(id,owner_user_id,first_name,last_name,deletion_pending,created_at,updated_at) values(:p,:u,'A','B',false,now(),now())"), {"p": p, "u": u})
        for i, (status, d) in enumerate(docs.items()):
            c.execute(text("insert into documents(id,patient_id,uploaded_by,filename,content_type,file_size,storage_key,checksum,document_type,status,created_at,updated_at) values(:d,:p,:u,'f.pdf','application/pdf',1,:k,:h,'UNKNOWN',:s,now(),now())"), {"d": d, "p": p, "u": u, "k": f"k{i}", "h": str(i) * 64, "s": status})
            c.execute(text("insert into document_text_blocks(id,document_id,page_number,block_index,source_text,extraction_method,created_at) values(:i,:d,1,0,'PHI text','pdf_text',now())"), {"i": str(uuid.uuid4()), "d": d})
    assert alembic(pg, "upgrade", "head").returncode == 0
    with e.connect() as c:
        left = dict(c.execute(text("select d.status, count(b.id) from documents d left join document_text_blocks b on b.document_id=d.id group by d.status")).all())
    assert left == {"DELETED": 0, "DELETION_PENDING": 1, "COMPLETED": 1}      # only the permanently deleted document was scrubbed
