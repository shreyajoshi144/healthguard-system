"""Release-audit regression tests. Each test documents a defect that was reproduced against the supplied
P0_FINAL package before the fix (see docs/RELEASE_AUDIT.md for the reproduction log)."""
from __future__ import annotations

from datetime import date

import pytest

from ai.answer_provider import AnswerClaim, AnswerResult
from db.models import *
from domain.grounding import _claim_supported, validate_grounded_answer
from domain.retrieval import (
    retrieve_patient_evidence,
    retrieve_patient_hybrid_evidence,
    retrieve_patient_semantic_evidence,
)
from tests.test_sprint5_phase1_security import clean, client, make_db, seed


# --------------------------------------------------------------------------------------------------
# AUDIT-001  Grounding validator accepted claims whose numbers/dates contradict the cited evidence.
# --------------------------------------------------------------------------------------------------
EVIDENCE_TEXT = "Hemoglobin 14.2 g/dL on 2024-03-01 (reference 13-17)"


@pytest.mark.parametrize("claim", [
    "Hemoglobin was 14.2 g/dL",
    "Hemoglobin was 14.20 g/dL",                       # trailing zero is the same number
    "Hemoglobin was 14.2 g/dL on 2024-03-01",
    "Hemoglobin was 14.2 g/dL on March 1, 2024",       # same date, different notation
    "Hemoglobin was 14.2 g/dL on 1 March 2024",
])
def test_claim_with_matching_numbers_and_dates_is_supported(claim):
    assert _claim_supported(claim, EVIDENCE_TEXT)


@pytest.mark.parametrize("claim", [
    "Hemoglobin was 9.0 g/dL",                          # wrong value
    "Hemoglobin was 4.2 g/dL",                          # digit dropped
    "Hemoglobin is 142 g/dL",                           # decimal point dropped
    "Hemoglobin was 14.2 g/dL on 2023-01-05",           # wrong date
    "Hemoglobin was 14.2 g/dL on March 2, 2024",        # wrong day, other notation
    "Hemoglobin was 14.2 g/dL on 2025-03-01",           # wrong year
])
def test_claim_with_wrong_number_or_date_is_rejected(claim):
    assert not _claim_supported(claim, EVIDENCE_TEXT)


def test_wrong_value_claim_never_reaches_the_patient_as_grounded():
    ev = [{"evidence_id": "e1", "patient_id": "p1", "source_type": "INVESTIGATION_RESULT",
           "source_text": "Hemoglobin 14.2 g/dL on 2024-03-01", "value": "14.2", "details": {},
           "provenance": "DOCUMENT_VERIFIED"}]
    bad = AnswerResult("x", [AnswerClaim("Hemoglobin was 9.0 g/dL on 2024-03-01", ["e1"])], ["e1"], True, None)
    v = validate_grounded_answer(bad, ev, expected_patient_id="p1")
    assert v.valid is False and v.grounded is False and v.reason == "unsupported claim"
    good = AnswerResult("x", [AnswerClaim("Hemoglobin was 14.2 g/dL on 2024-03-01", ["e1"])], ["e1"], True, None)
    assert validate_grounded_answer(good, ev, expected_patient_id="p1").grounded is True


# --------------------------------------------------------------------------------------------------
# AUDIT-002  Retrieval (Ask / search / handoff) kept serving facts and extracted text of deleted documents.
# --------------------------------------------------------------------------------------------------
@pytest.fixture
def deleted_env():
    S = make_db()
    db, a, b, c, p, q = seed(S)

    def doc(name, status, key):
        d = Document(patient_id=p.id, uploaded_by=a.id, filename=name, content_type="application/pdf", file_size=1,
                     storage_key=key, checksum=key * 64, status=status, document_type=DocumentType.LAB_REPORT)
        db.add(d); db.flush(); return d

    live = doc("live.pdf", DocumentStatus.COMPLETED, "1")
    gone = doc("gone.pdf", DocumentStatus.DELETED, "2")
    pend = doc("pending.pdf", DocumentStatus.DELETION_PENDING, "3")
    for d, tag in ((live, "LIVEMARK"), (gone, "GONEMARK"), (pend, "PENDMARK")):
        db.add(HealthFact(patient_id=p.id, document_id=d.id, fact_type="DIAGNOSIS", value=f"Diagnosis: {tag} asthma",
                          source_page=1, source_text=f"Dx {tag} asthma", extraction_method="rule"))
        db.add(DocumentTextBlock(document_id=d.id, page_number=1, block_index=0, source_text=f"asthma note {tag}", extraction_method="pdf_text"))
    # A user-entered event that merely cites the deleted document must survive, but must not cite it.
    db.add(HealthEvent(patient_id=p.id, event_type=EventType.CONSULTATION, title="asthma clinic visit", event_date=date(2024, 3, 1),
                       date_precision=DatePrecision.EXACT, created_by=a.id, source_type=EventSource.DOCUMENT, source_id=gone.id,
                       provenance=Provenance.DOCUMENT_VERIFIED))
    db.commit()
    yield db, a, p, gone, pend
    clean()


def _blob(rows) -> str:
    return " ".join(str(r) for r in rows)


@pytest.mark.parametrize("retriever", [retrieve_patient_evidence, retrieve_patient_semantic_evidence, retrieve_patient_hybrid_evidence])
def test_retrieval_never_returns_content_of_deleted_or_pending_deletion_documents(deleted_env, retriever):
    db, a, p, gone, pend = deleted_env
    blob = _blob(retriever(db, p.id, "asthma", limit=50))
    assert "LIVEMARK" in blob, "control: live document content must still be retrievable"
    assert "GONEMARK" not in blob and "PENDMARK" not in blob


def test_event_sourced_from_deleted_document_is_kept_but_does_not_cite_the_document(deleted_env):
    db, a, p, gone, pend = deleted_env
    rows = [r for r in retrieve_patient_evidence(db, p.id, "asthma clinic visit", limit=50) if r["source_type"] == "HEALTH_EVENT"]
    assert rows, "the patient's own event entry must remain retrievable"
    assert all(r["document_id"] is None for r in rows)


def test_retrieve_api_does_not_leak_deleted_document_content(deleted_env):
    db, a, p, gone, pend = deleted_env
    r = client(db, a).post(f"/api/patients/{p.id}/retrieve", json={"query": "asthma", "limit": 50})
    assert r.status_code == 200
    assert "GONEMARK" not in r.text and "PENDMARK" not in r.text and "LIVEMARK" in r.text


# --------------------------------------------------------------------------------------------------
# AUDIT-003  Deleting a document removed the stored file but left its full extracted text and facts in the database.
# --------------------------------------------------------------------------------------------------
@pytest.fixture
def purge_env(monkeypatch):
    S = make_db()
    db, a, b, c, p, q = seed(S)
    d = Document(patient_id=p.id, uploaded_by=a.id, filename="x.pdf", content_type="application/pdf", file_size=1, storage_key="k-purge",
                 checksum="9" * 64, status=DocumentStatus.COMPLETED, document_type=DocumentType.LAB_REPORT)
    db.add(d); db.flush()
    db.add(DocumentTextBlock(document_id=d.id, page_number=1, block_index=0, source_text="FULL OCR TEXT", extraction_method="pdf_text"))
    f = HealthFact(patient_id=p.id, document_id=d.id, fact_type="DIAGNOSIS", value="Diagnosis: asthma", source_page=1, source_text="asthma", extraction_method="rule")
    keep = HealthEvent(patient_id=p.id, event_type=EventType.OTHER, title="user-entered", created_by=a.id)
    db.add_all([f, keep]); db.flush()
    act = HealthAction(patient_id=p.id, title="Follow up", action_type="FOLLOW_UP", source=HealthActionSource.DOCUMENT_DERIVED,
                       provenance=Provenance.DOCUMENT_VERIFIED, status=HealthActionStatus.PLANNED, source_document_id=d.id, source_fact_id=f.id)
    db.add(act); db.commit()
    import api.healthguard as hg
    deleted_keys: list[str] = []
    monkeypatch.setattr(hg.storage, "delete", lambda key: deleted_keys.append(key))
    yield db, a, p, d, act, deleted_keys
    clean()


def test_document_deletion_purges_extracted_text_and_facts_but_keeps_user_entries(purge_env):
    db, a, p, d, act, deleted_keys = purge_env
    assert client(db, a).delete(f"/api/documents/{d.id}").status_code == 204
    db.expire_all()
    assert deleted_keys == ["k-purge"]
    assert db.get(Document, d.id).status == DocumentStatus.DELETED
    assert db.query(DocumentTextBlock).filter_by(document_id=d.id).count() == 0
    assert db.query(HealthFact).filter_by(document_id=d.id).count() == 0
    assert db.query(HealthEvent).filter_by(patient_id=p.id, title="user-entered").count() == 1
    kept = db.get(HealthAction, act.id)
    assert kept is not None and kept.source_fact_id is None            # FK SET NULL, no dangling reference


def test_failed_storage_delete_keeps_derived_content_until_retry_succeeds(purge_env, monkeypatch):
    db, a, p, d, act, deleted_keys = purge_env
    import api.healthguard as hg
    monkeypatch.setattr(hg.storage, "delete", lambda key: (_ for _ in ()).throw(RuntimeError("s3 down")))
    assert client(db, a).delete(f"/api/documents/{d.id}").status_code == 503
    db.expire_all()
    assert db.get(Document, d.id).status == DocumentStatus.DELETION_PENDING      # never reported as permanently deleted
    assert db.query(DocumentTextBlock).filter_by(document_id=d.id).count() == 1  # not purged until the file is really gone
    monkeypatch.setattr(hg.storage, "delete", lambda key: deleted_keys.append(key))
    assert client(db, a).delete(f"/api/documents/{d.id}").status_code == 204
    db.expire_all()
    assert db.query(DocumentTextBlock).filter_by(document_id=d.id).count() == 0


def test_repeating_delete_of_an_already_deleted_document_scrubs_residue(purge_env):
    db, a, p, d, act, deleted_keys = purge_env
    d.status = DocumentStatus.DELETED; db.commit()                                # state produced by the pre-fix code
    assert client(db, a).delete(f"/api/documents/{d.id}").status_code == 204
    db.expire_all()
    assert db.query(DocumentTextBlock).filter_by(document_id=d.id).count() == 0
    assert db.query(HealthFact).filter_by(document_id=d.id).count() == 0


# --------------------------------------------------------------------------------------------------
# AUDIT-004  Usernames were matched as SQL LIKE patterns at login and registration ('%', 'ali%', 'a_ice' all matched 'alice').
# --------------------------------------------------------------------------------------------------
@pytest.fixture
def accounts():
    from backend import auth
    S = make_db(); db = S()

    def mk(name, pw, email):
        u = auth.register_user(db, name, email, pw); u.email_verified = True; db.commit(); return u
    return auth, db, mk


@pytest.mark.parametrize("identifier", ["%", "ali%", "a_ice", "_lice", "alic_", "%lice"])
def test_login_identifier_is_matched_exactly_not_as_a_like_pattern(accounts, identifier):
    auth, db, mk = accounts
    mk("alice", "AlicePass123!", "alice@example.com")
    with pytest.raises(auth.AuthError):
        auth.authenticate_user(db, identifier, "AlicePass123!")


def test_login_is_still_case_insensitive_and_email_login_still_works(accounts):
    auth, db, mk = accounts
    mk("Alice", "AlicePass123!", "alice@example.com")
    assert auth.authenticate_user(db, "ALICE", "AlicePass123!").username == "Alice"
    assert auth.authenticate_user(db, "alice@example.com", "AlicePass123!").username == "Alice"


def test_wildcard_lookalike_usernames_are_distinct_accounts(accounts):
    auth, db, mk = accounts
    mk("alice", "AlicePass123!", "alice@example.com")
    mk("a_ice", "OtherPass123!", "other@example.com")                      # was rejected as a false duplicate
    assert auth.authenticate_user(db, "a_ice", "OtherPass123!").username == "a_ice"
    assert auth.authenticate_user(db, "alice", "AlicePass123!").username == "alice"
    with pytest.raises(auth.AuthError):
        auth.authenticate_user(db, "a_ice", "AlicePass123!")              # alice's password must not open a_ice


def test_real_duplicates_are_still_rejected_case_insensitively(accounts):
    auth, db, mk = accounts
    mk("alice", "AlicePass123!", "alice@example.com")
    with pytest.raises(auth.AuthError):
        mk("ALICE", "AlicePass123!", "second@example.com")


def test_escape_like_neutralises_wildcards():
    from utils.sql import escape_like
    assert escape_like("a_b%c\\d") == "a\\_b\\%c\\\\d"


# --------------------------------------------------------------------------------------------------
# AUDIT-005  The request-body limit trusted Content-Length, so a chunked upload bypassed it entirely.
# --------------------------------------------------------------------------------------------------
def _chunks(total_mb: int):
    yield b'{"username":"x","password":"'
    for _ in range(total_mb):
        yield b"A" * (1 << 20)
    yield b'"}'


@pytest.fixture
def anon_app():
    from backend.main import app
    from db.session import get_db
    db = make_db()()
    app.dependency_overrides[get_db] = lambda: db
    yield app
    clean()


def test_oversized_chunked_body_is_rejected_without_content_length(anon_app):
    from fastapi.testclient import TestClient
    from backend.main import MAX_REQUEST_BODY_BYTES
    app = anon_app
    mb = MAX_REQUEST_BODY_BYTES // (1 << 20) + 8
    with TestClient(app) as c:
        r = c.post("/api/auth/login", content=_chunks(mb), headers={"content-type": "application/json"})
    assert r.status_code == 413, r.text


def test_normal_chunked_body_and_content_length_body_still_work(anon_app):
    from fastapi.testclient import TestClient
    app = anon_app
    with TestClient(app) as c:
        assert c.post("/api/auth/login", content=_chunks(0), headers={"content-type": "application/json"}).status_code == 401
        assert c.post("/api/auth/login", json={"username": "x", "password": "y"}).status_code == 401


# --------------------------------------------------------------------------------------------------
# AUDIT-006  A hung Redis froze the whole API (no socket timeout, blocking call inside an async dependency),
#            and INCR+EXPIRE was not atomic (a crash between them leaves a counter that never expires).
# --------------------------------------------------------------------------------------------------
import asyncio
import shutil
import socket
import subprocess
import threading
import time


class _Req:
    headers: dict = {}
    cookies: dict = {}
    client = type("C", (), {"host": "203.0.113.7"})()


@pytest.fixture
def blackhole_redis():
    """A TCP endpoint that accepts connections and never answers: what a hung Redis looks like to the client."""
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(32)
    held, stop = [], threading.Event()

    def loop():
        srv.settimeout(0.2)
        while not stop.is_set():
            try: held.append(srv.accept()[0])
            except OSError: pass
    t = threading.Thread(target=loop, daemon=True); t.start()
    yield f"redis://127.0.0.1:{srv.getsockname()[1]}/0"
    stop.set(); srv.close()
    for c in held: c.close()


@pytest.fixture
def real_redis():
    exe = shutil.which("redis-server")
    if not exe:
        pytest.skip("redis-server binary not installed in this environment")        # reported as SKIPPED, never as a pass
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    proc = subprocess.Popen([exe, "--port", str(port), "--save", "", "--appendonly", "no"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    import redis as redislib
    url = f"redis://127.0.0.1:{port}/0"
    for _ in range(50):
        try: redislib.from_url(url).ping(); break
        except Exception: time.sleep(0.1)
    yield url
    proc.terminate(); proc.wait(5)


def test_hung_redis_does_not_block_the_event_loop_and_production_fails_closed(monkeypatch, blackhole_redis):
    import domain.rate_limit as rl
    from fastapi import HTTPException
    monkeypatch.setenv("APP_ENV", "production"); monkeypatch.setenv("REDIS_URL", blackhole_redis)
    monkeypatch.setenv("HEALTHGUARD_REDIS_SOCKET_TIMEOUT_SECONDS", "0.3"); monkeypatch.setenv("HEALTHGUARD_REDIS_CONNECT_TIMEOUT_SECONDS", "0.3")
    dep = rl.rate_limit("audit-hung", 5)

    out: dict = {}

    def run_scenario():                                       # own thread + own loop so a blocked loop cannot hang the test run
        async def scenario():
            ticks = 0

            async def ticker():                               # stands in for /api/health and every other request
                nonlocal ticks
                while True:
                    await asyncio.sleep(0.02); ticks += 1
            tk = asyncio.create_task(ticker())
            t0 = time.monotonic()
            try:
                await dep(_Req())
                out["status"] = 200
            except HTTPException as e:
                out["status"] = e.status_code
            out["elapsed"] = time.monotonic() - t0; out["ticks"] = ticks
            tk.cancel()
        asyncio.run(scenario())
    th = threading.Thread(target=run_scenario, daemon=True); th.start(); th.join(timeout=15)
    assert not th.is_alive(), "rate limiter blocked the event loop on a hung Redis for more than 15s"
    assert out["status"] == 503
    assert out["elapsed"] < 3.0, f"hung Redis held the request for {out['elapsed']:.1f}s"
    assert out["ticks"] >= 10, f"event loop was blocked while waiting on Redis (only {out['ticks']} ticks)"


def test_redis_client_is_cached_with_bounded_timeouts(monkeypatch):
    import domain.rate_limit as rl
    made = []

    class FakeModule:
        @staticmethod
        def from_url(url, **kw): made.append(kw); return object()
    monkeypatch.setattr(rl, "redis", FakeModule); rl._clients.clear()
    monkeypatch.setenv("HEALTHGUARD_REDIS_SOCKET_TIMEOUT_SECONDS", "0.25")
    a = rl._redis_client("redis://x:1/0"); b = rl._redis_client("redis://x:1/0")
    assert a is b and len(made) == 1, "one pooled client must be reused, not a new connection per request"
    assert 0 < made[0]["socket_timeout"] <= 1 and 0 < made[0]["socket_connect_timeout"] <= 1 and made[0]["health_check_interval"] > 0


def test_real_redis_counts_limits_and_always_sets_a_ttl(monkeypatch, real_redis):
    import domain.rate_limit as rl
    import redis as redislib
    from fastapi import HTTPException
    monkeypatch.setenv("REDIS_URL", real_redis); monkeypatch.setenv("APP_ENV", "production"); rl._clients.clear()
    dep = rl.rate_limit("audit-real", 3)

    async def run():
        for _ in range(3): await dep(_Req())
        with pytest.raises(HTTPException) as e:
            await dep(_Req())
        return e.value.status_code
    assert asyncio.run(run()) == 429
    r = redislib.from_url(real_redis)
    keys = r.keys("healthguard:rl:audit-real:*"); assert len(keys) == 1
    assert 0 < r.ttl(keys[0]) <= rl.WINDOW
    r.persist(keys[0])                                         # simulate a crash between INCR and EXPIRE
    assert r.ttl(keys[0]) == -1
    with pytest.raises(HTTPException): asyncio.run(dep(_Req()))
    assert 0 < r.ttl(keys[0]) <= rl.WINDOW, "a counter without a TTL must self-heal instead of locking the caller out forever"


# --------------------------------------------------------------------------------------------------
# AUDIT-007  Session tokens without an expiry claim were accepted forever.
# --------------------------------------------------------------------------------------------------
def test_token_without_expiry_is_rejected_and_normal_tokens_still_work():
    import jwt
    from backend import auth
    forever = jwt.encode({"sub": "00000000-0000-0000-0000-000000000001", "sv": 1}, auth.SECRET_KEY, algorithm=auth.ALGORITHM)
    with pytest.raises(auth.AuthError):
        auth.decode_access_token(forever)
    normal = auth.create_access_token("00000000-0000-0000-0000-000000000001")
    assert auth.decode_access_token(normal) == "00000000-0000-0000-0000-000000000001"


# --------------------------------------------------------------------------------------------------
# AUDIT-008  On a clean checkout 7 reliability tests were silently skipped; "0 skipped" only held after a prior run.
# --------------------------------------------------------------------------------------------------
def test_reliability_suite_inputs_are_available_so_it_cannot_silently_skip():
    from tests.test_reliability import _real_data_available
    assert _real_data_available(), "Phase 3 reliability inputs missing (splits, frozen models, or spaCy en_core_web_md): refusing to skip silently"


# --------------------------------------------------------------------------------------------------
# AUDIT-009  The shipped package contained a runtime log; nothing enforced a clean release tree.
# --------------------------------------------------------------------------------------------------
def test_release_package_excludes_runtime_artifacts_and_keeps_source(tmp_path):
    import zipfile
    from scripts.make_release import build_release
    src = tmp_path / "hg"
    keep = ["backend/main.py", "alembic/versions/0001_x.py", "models/m/model.joblib", ".env.example", "docs/README.md", "data/sample.csv"]
    drop = ["healthguard.db", "x.sqlite3", "logs/app.log", "a.log", "backend/__pycache__/m.cpython-312.pyc", ".pytest_cache/v/x", "data/splits/train.csv",
            "private_storage/doc.bin", ".env", ".env.production", "secrets/server.pem", "node_modules/x/index.js", ".git/config", "tls.key"]
    for rel in keep + drop:
        f = src / rel; f.parent.mkdir(parents=True, exist_ok=True); f.write_text("x")
    out = tmp_path / "rel.zip"
    build_release(src, out)
    names = {n.split("/", 1)[1] for n in zipfile.ZipFile(out).namelist()}
    assert names == set(keep), f"unexpected: {names - set(keep)}; missing: {set(keep) - names}"
    out2 = tmp_path / "rel2.zip"; build_release(src, out2)
    assert out.read_bytes() == out2.read_bytes(), "release build must be deterministic"
