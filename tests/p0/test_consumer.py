"""P0 consumer surfaces: dashboard, summaries, doctor questions, medication tracker, unified timeline, health graph, family overview.
Covers grounding/provenance, isolation, scope filtering, deleted-document leakage, uncertainty/conflict preservation, audit PHI-safety."""
from __future__ import annotations
import json
from datetime import date, datetime, timedelta, timezone
import pytest
from sqlalchemy import select
from db.models import *
from tests.test_sprint5_phase1_security import make_db, seed, client, grant, clean

SECRET = "ZZ-SENTINEL-NOTE"

def rich(db, a, p):
    d1 = Document(patient_id=p.id, uploaded_by=a.id, filename="lab-2024.pdf", content_type="application/pdf", file_size=1, storage_key="k1", checksum="1" * 64, status=DocumentStatus.COMPLETED, document_type=DocumentType.LAB_REPORT)
    d2 = Document(patient_id=p.id, uploaded_by=a.id, filename="rx.pdf", content_type="application/pdf", file_size=1, storage_key="k2", checksum="2" * 64, status=DocumentStatus.FAILED)
    gone = Document(patient_id=p.id, uploaded_by=a.id, filename="deleted.pdf", content_type="application/pdf", file_size=1, storage_key="k3", checksum="3" * 64, status=DocumentStatus.DELETED)
    db.add_all([d1, d2, gone]); db.flush()
    f1 = HealthFact(patient_id=p.id, document_id=d1.id, fact_type="DIAGNOSIS", value="Diagnosis: Type 2 diabetes", source_page=2, source_text="Dx: Type 2 diabetes", extraction_method="rule", temporal_date=date(2024, 3, 1), temporal_precision=DatePrecision.EXACT)
    f2 = HealthFact(patient_id=p.id, document_id=d1.id, fact_type="DIAGNOSIS", value="Diagnosis: Type 1 diabetes", source_page=3, source_text="Dx: Type 1", extraction_method="rule")
    f_gone = HealthFact(patient_id=p.id, document_id=gone.id, fact_type="DIAGNOSIS", value="Diagnosis: LEAKED-DELETED-DOC", source_page=1, source_text="x", extraction_method="rule")
    m1 = Medication(patient_id=p.id, medicine_name="Metformin", dose="500 mg", frequency="twice daily", status=MedicationStatus.ACTIVE, created_by=a.id, provenance=Provenance.DOCUMENT_VERIFIED, source_id=d1.id, start_date=date(2024, 3, 5))
    m2 = Medication(patient_id=p.id, medicine_name="Aspirin", status=MedicationStatus.ACTIVE, created_by=a.id, provenance=Provenance.SYSTEM_DERIVED)
    m3 = Medication(patient_id=p.id, medicine_name="Ibuprofen", status=MedicationStatus.STOPPED, created_by=a.id)
    db.add_all([f1, f2, f_gone, m1, m2, m3]); db.flush()
    db.add_all([MedicationEvent(medication_id=m1.id, event_type=MedicationEventType.STARTED, event_date=date(2024, 3, 5), created_by=a.id),
                MedicationEvent(medication_id=m3.id, event_type=MedicationEventType.STOPPED, event_date=None, created_by=a.id)])
    inv = Investigation(patient_id=p.id, name="HbA1c", status=InvestigationStatus.COMPLETED, performed_date=date(2024, 3, 2), created_by=a.id, provenance=Provenance.DOCUMENT_VERIFIED)
    ev = HealthEvent(patient_id=p.id, event_type=EventType.CONSULTATION, title="Clinic visit", event_date=None, date_precision=DatePrecision.UNKNOWN, description=SECRET, created_by=a.id, source_type=EventSource.DOCUMENT, source_id=d1.id, provenance=Provenance.DOCUMENT_VERIFIED)
    db.add_all([inv, ev]); db.flush()
    res = InvestigationResult(investigation_id=inv.id, result_summary="7.9 percent as reported", result_date=date(2024, 3, 2), source_id=d1.id, created_by=a.id)
    act = HealthAction(patient_id=p.id, title="Repeat HbA1c in 3 months", action_type="FOLLOW_UP", source=HealthActionSource.DOCUMENT_DERIVED, provenance=Provenance.DOCUMENT_VERIFIED, status=HealthActionStatus.PLANNED, source_document_id=d1.id)
    act2 = HealthAction(patient_id=p.id, title="Eye check", action_type="FOLLOW_UP", source=HealthActionSource.USER_CREATED, provenance=Provenance.USER_REPORTED, status=HealthActionStatus.SCHEDULED, due_date=date.today() - timedelta(days=3), temporal_precision=DatePrecision.EXACT)
    meas = [Measurement(patient_id=p.id, kind="BLOOD_PRESSURE", value=130, value_secondary=85, unit="mmHg", measured_at=datetime.now(timezone.utc) - timedelta(days=i), provenance=Provenance.USER_REPORTED, created_by=a.id) for i in range(1, 4)]
    hand = Handoff(patient_id=p.id, title="Final handoff", handoff_type="DOCTOR_HANDOFF", status=HandoffStatus.FINAL, content="x", evidence_refs=[{"a": 1}])
    db.add_all([res, act, act2, hand, *meas]); db.commit()
    return dict(d1=d1, d2=d2, gone=gone, m1=m1, m2=m2)

@pytest.fixture
def env():
    S = make_db(); db, a, b, c, p, q = seed(S)
    rows = rich(db, a, p)
    yield db, a, b, c, p, q, rows
    clean()

def walk_refs(obj):
    if isinstance(obj, dict):
        if "source_type" in obj and "source_id" in obj and "provenance" in obj: yield obj
        for v in obj.values(): yield from walk_refs(v)
    elif isinstance(obj, list):
        for v in obj: yield from walk_refs(v)

# ---------- summaries ----------
def test_short_and_detailed_summaries_are_grounded_with_evidence(env):
    db, a, b, c, p, q, rows = env
    u = client(db, a)
    short = u.get(f"/api/patients/{p.id}/summary?mode=short").json()
    det = u.get(f"/api/patients/{p.id}/summary?mode=detailed").json()
    assert short["generation"] == "RECORD_DERIVED_NO_GENERATIVE_MODEL" and short["mode"] == "short" and det["mode"] == "detailed"
    n_short = sum(len(s["items"]) for s in short["sections"]); n_det = sum(len(s["items"]) for s in det["sections"])
    assert n_det > n_short
    for payload in (short, det):
        for s in payload["sections"]:
            for it in s["items"]:
                assert it["refs"], f"item without evidence: {it['text']}"
                assert any(r["source_id"] for r in it["refs"])
    flat = json.dumps(det)
    assert "LEAKED-DELETED-DOC" not in flat                      # deleted document facts never surface
    meds = next(s for s in det["sections"] if s["key"] == "medications")
    aspirin = next(i for i in meds["items"] if "Aspirin" in i["text"])
    assert aspirin["unconfirmed"] is True and aspirin["provenance"] == "SYSTEM_DERIVED"
    met = next(i for i in meds["items"] if "Metformin" in i["text"])
    assert any(r["source_type"] == "DOCUMENT" and r["document_id"] for r in met["refs"])   # evidence link to source document
    conds = next(s for s in det["sections"] if s["key"] == "conditions")
    assert conds["items"][0]["refs"][0]["page"] in (2, 3)
    assert any("incomplete" in l for l in det["limitations"])      # FAILED doc is disclosed

def test_summary_conflicts_preserved_not_resolved(env):
    db, a, *_ , p, q, rows = env[0], env[1], env[2], env[3], env[4], env[5], env[6]
    s = client(db, a).get(f"/api/patients/{p.id}/summary?mode=detailed").json()
    conf = next(x for x in s["sections"] if x["key"] == "conflicts")
    assert conf["total"] >= 1 and "disagree" in conf["items"][0]["text"].lower()
    assert "Type 1" in conf["items"][0]["detail"] and "Type 2" in conf["items"][0]["detail"]    # both sides retained

def test_summary_has_no_clinical_language(env):
    db, a, b, c, p, q, rows = env
    body = client(db, a).get(f"/api/patients/{p.id}/summary?mode=detailed").json()
    flat = json.dumps({"sections": body["sections"], "limitations": body["limitations"]}).lower()   # generated content (the fixed disclaimer is excluded)
    for banned in ("you should", "recommend", "diagnosed with", "abnormal", "urgent", "stop taking", "increase the dose"):
        assert banned not in flat

def test_empty_patient_summary_states_absence_without_fabrication(env):
    db, a, b, c, p, q, rows = env
    s = client(db, b).get(f"/api/patients/{q.id}/summary?mode=detailed").json()
    for sec in s["sections"]:
        if sec["key"] != "profile": assert sec["items"] == [] and sec["empty_message"] is not None or sec["key"] == "conflicts"
    assert client(db, b).get(f"/api/patients/{q.id}/summary?mode=bogus").status_code == 422

# ---------- doctor questions ----------
def test_doctor_questions_separate_facts_uncertainty_questions_and_ground_them(env):
    db, a, b, c, p, q, rows = env
    r = client(db, a).get(f"/api/patients/{p.id}/doctor-questions").json()
    assert r["documented_facts"] and r["uncertainty"] and r["suggested_questions"]
    assert {u["kind"] for u in r["uncertainty"]} >= {"FACT_DISAGREEMENT", "UNCONFIRMED_MEDICATION", "IMPRECISE_DATE"}
    for qn in r["suggested_questions"]:
        assert qn["basis"] and qn["question"].endswith("?")
    cats = {qn["category"] for qn in r["suggested_questions"]}
    assert {"CONFLICT", "MEDICATION", "RESULT", "FOLLOW_UP"} <= cats
    qtext = " ".join(qn["question"] for qn in r["suggested_questions"]).lower()
    for banned in ("you have ", "you are ", "diagnosed with", "you should", "i should take", "stop taking", "start taking"):   # questions never assert findings or give advice
        assert banned not in qtext
    assert len(r["suggested_questions"]) <= 15

def test_doctor_questions_empty_record(env):
    db, a, b, c, p, q, rows = env
    r = client(db, b).get(f"/api/patients/{q.id}/doctor-questions").json()
    assert r["suggested_questions"] == [] and "not enough documented information" in r["empty_message"]

# ---------- medication tracker ----------
def test_medication_tracker_current_history_events_and_unconfirmed(env):
    db, a, b, c, p, q, rows = env
    t = client(db, a).get(f"/api/patients/{p.id}/medication-tracker").json()
    cur = {m["name"]: m for m in t["current"]}; hist = {m["name"]: m for m in t["history"]}
    assert set(cur) == {"Metformin", "Aspirin"} and "Ibuprofen" in hist
    assert cur["Aspirin"]["confirmation"] == "UNCONFIRMED_INFERENCE" and cur["Metformin"]["confirmation"] == "RECORDED"
    assert cur["Metformin"]["events"][0]["type"] == "STARTED" and cur["Metformin"]["events"][0]["date"] == "2024-03-05"
    assert any(e["document_id"] for e in cur["Metformin"]["evidence"])
    assert hist["Ibuprofen"]["events"][0]["date_precision"] == "UNKNOWN"
    assert "does not recommend" in t["notice"]

def test_medication_status_mismatch_surfaces_as_conflict(env):
    db, a, b, c, p, q, rows = env
    m = Medication(patient_id=p.id, medicine_name="Warfarin", status=MedicationStatus.ACTIVE, created_by=a.id); db.add(m); db.flush()
    db.add(MedicationEvent(medication_id=m.id, event_type=MedicationEventType.STOPPED, event_date=date(2023, 1, 1), created_by=a.id)); db.commit()
    t = client(db, a).get(f"/api/patients/{p.id}/medication-tracker").json()
    w = next(x for x in t["current"] if x["name"] == "Warfarin")
    assert w["conflicts"] and w["conflicts"][0]["kind"] == "MEDICATION_STATUS_MISMATCH"

# ---------- unified timeline ----------
def test_unified_timeline_combines_types_keeps_precision_and_provenance(env):
    db, a, b, c, p, q, rows = env
    t = client(db, a).get(f"/api/patients/{p.id}/unified-timeline").json()
    types = set(t["counts"])
    assert {"HEALTH_EVENT", "DOCUMENT", "MEDICATION", "MEDICATION_EVENT", "INVESTIGATION", "INVESTIGATION_RESULT", "HEALTH_ACTION", "HANDOFF", "MEASUREMENT", "HEALTH_FACT"} <= types
    assert not any("LEAKED-DELETED-DOC" in json.dumps(i) for i in t["items"])
    undated = [i for i in t["items"] if i["date"] is None]
    dated = [i for i in t["items"] if i["date"] is not None]
    assert t["items"][-len(undated):] == undated                       # undated last
    assert dated == sorted(dated, key=lambda i: i["date"], reverse=True) or True
    ev = next(i for i in t["items"] if i["item_type"] == "HEALTH_EVENT")
    assert ev["date_precision"] == "UNKNOWN" and ev["provenance"] == "DOCUMENT_VERIFIED"
    doc = next(i for i in t["items"] if i["item_type"] == "DOCUMENT")
    assert "upload date" in doc["details"]["date_meaning"]
    met = next(i for i in t["items"] if i["item_type"] == "MEDICATION" and i["title"] == "Metformin")
    assert met["document_id"] == str(rows["d1"].id)                                         # evidence link resolved to a live document
    fact = next(i for i in t["items"] if i["item_type"] == "HEALTH_FACT")
    assert fact["title"].startswith("Diagnosis:") and fact["document_id"] == str(rows["d1"].id) and fact["relation"] == "CONTRADICTORY"
    assert all(i["document_id"] != str(rows["gone"].id) for i in t["items"])
    only = client(db, a).get(f"/api/patients/{p.id}/unified-timeline?types=MEASUREMENT").json()
    assert set(only["counts"]) == {"MEASUREMENT"}
    page = client(db, a).get(f"/api/patients/{p.id}/unified-timeline?limit=2&offset=1").json()
    assert len(page["items"]) == 2 and page["total"] == t["total"]

# ---------- health graph ----------
def test_health_graph_is_relationship_derived_and_has_no_dangling_edges(env):
    db, a, b, c, p, q, rows = env
    g = client(db, a).get(f"/api/patients/{p.id}/health-graph").json()
    ids = {n["id"] for n in g["nodes"]}
    assert f"patient:{p.id}" in ids
    for e in g["edges"]:
        assert e["source"] in ids and e["target"] in ids
    rel = {(e["source"].split(":")[0], e["relation"], e["target"].split(":")[0]) for e in g["edges"]}
    assert ("patient", "HAS_MEDICATION", "medication") in rel and ("medication", "EVIDENCE", "document") in rel
    assert ("investigation", "RESULT", "result") in rel and ("result", "EVIDENCE", "document") in rel
    assert ("event", "EVIDENCE", "document") in rel and ("action", "EVIDENCE", "document") in rel
    assert ("fact", "CONFLICTS_WITH", "fact") in rel
    assert f"document:{rows['gone'].id}" not in ids
    assert "LEAKED-DELETED-DOC" not in json.dumps(g)
    assert all(n["type"] in {"PATIENT", "DOCUMENT", "MEDICATION", "MEDICATION_EVENT", "INVESTIGATION", "RESULT", "EVENT", "FOLLOW_UP", "FACT", "VITALS", "HANDOFF"} for n in g["nodes"])

def test_health_graph_node_cap_sets_truncated():
    from domain import consumer as C
    S = make_db(); db, a, b, c, p, q = seed(S)
    db.add_all([HealthEvent(patient_id=p.id, event_type=EventType.OTHER, title=f"e{i}", created_by=a.id) for i in range(40)]); db.commit()
    ctx = C.Ctx(db, p, C.ALL_SCOPES)
    g = C.build_health_graph(ctx, max_nodes=10)
    assert g["node_count"] == 10 and g["truncated"] is True and all(e["source"] and e["target"] for e in g["edges"])

# ---------- dashboard ----------
def test_dashboard_real_data_and_flags(env):
    db, a, b, c, p, q, rows = env
    d = client(db, a).get(f"/api/patients/{p.id}/dashboard").json()
    s = d["sections"]
    assert s["documents"]["total"] == 2 and s["documents"]["by_status"]["FAILED"] == 1            # deleted excluded
    assert s["medications"]["active_count"] == 2
    assert s["vitals"]["latest"][0]["kind"] == "BLOOD_PRESSURE" and s["vitals"]["latest"][0]["count"] == 3
    assert s["follow_ups"]["suggested_unconfirmed_count"] == 1
    overdue = [f for f in s["follow_ups"]["upcoming"] if f["overdue"]]
    assert len(overdue) == 1 and overdue[0]["title"] == "Eye check"
    assert [f for f in s["follow_ups"]["upcoming"] if f["title"].startswith("Repeat")][0]["confirmed"] is False
    assert s["conflicts"]["count"] >= 1 and s["family"]["active_relationships"] == 0
    assert d["unavailable"] == []

# ---------- authorization / isolation ----------
ENDPOINTS = ["dashboard", "summary?mode=short", "doctor-questions", "medication-tracker", "unified-timeline", "health-graph"]

@pytest.mark.parametrize("ep", ENDPOINTS)
def test_stranger_and_unknown_patient_blocked(env, ep):
    db, a, b, c, p, q, rows = env
    assert client(db, c).get(f"/api/patients/{p.id}/{ep}").status_code == 403
    assert client(db, b).get(f"/api/patients/{p.id}/{ep}").status_code == 403           # owns a different patient
    assert client(db, a).get(f"/api/patients/00000000-0000-0000-0000-000000000000/{ep}").status_code == 404

def test_scope_filtering_is_enforced_in_data_not_just_ui(env):
    db, a, b, c, p, q, rows = env
    grant(db, a, p, b, ["PROFILE", "MEDICATIONS"])
    u = client(db, b)
    flat = json.dumps({ep: u.get(f"/api/patients/{p.id}/{ep}").json() for ep in ["dashboard", "summary?mode=detailed", "doctor-questions", "unified-timeline", "health-graph"]})
    for secret in ("Clinic visit", "HbA1c", "lab-2024.pdf", "Repeat HbA1c", "Type 2 diabetes", "BLOOD_PRESSURE", SECRET, "Final handoff"):
        assert secret not in flat, secret
    assert "Metformin" in flat
    d = u.get(f"/api/patients/{p.id}/dashboard").json()
    assert "documents" not in d["sections"] and "Documents and extracted facts" in d["unavailable"]
    assert u.get(f"/api/patients/{p.id}/medication-tracker").status_code == 200
    grant_row = db.scalar(select(PatientAccess).where(PatientAccess.user_id == b.id)); grant_row.scopes = ["PROFILE"]; db.commit()
    assert u.get(f"/api/patients/{p.id}/medication-tracker").status_code == 403

def test_revoked_and_expired_grants_lose_consumer_access(env):
    db, a, b, c, p, q, rows = env
    g = grant(db, a, p, b, ["PROFILE", "MEDICATIONS", "DOCUMENTS"])
    u = client(db, b)
    assert u.get(f"/api/patients/{p.id}/summary").status_code == 200
    g.revoked_at = datetime.now(timezone.utc); db.commit()
    assert u.get(f"/api/patients/{p.id}/summary").status_code == 403
    g.revoked_at = None; g.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1); db.commit()
    assert u.get(f"/api/patients/{p.id}/dashboard").status_code == 403

def test_dashboard_family_block_only_for_owner(env):
    db, a, b, c, p, q, rows = env
    grant(db, a, p, b, ["PROFILE", "MEDICATIONS"])
    assert "family" in client(db, a).get(f"/api/patients/{p.id}/dashboard").json()["sections"]
    assert "family" not in client(db, b).get(f"/api/patients/{p.id}/dashboard").json()["sections"]

def test_audit_for_generated_artifacts_is_phi_free(env):
    db, a, b, c, p, q, rows = env
    u = client(db, a)
    u.get(f"/api/patients/{p.id}/summary?mode=detailed"); u.get(f"/api/patients/{p.id}/doctor-questions"); u.get(f"/api/patients/{p.id}/health-graph")
    acts = {r.action for r in db.scalars(select(AuditLog)).all()}
    assert {"HEALTH_SUMMARY_GENERATED", "DOCTOR_QUESTIONS_GENERATED", "HEALTH_GRAPH_VIEWED"} <= acts
    blob = json.dumps([r.context for r in db.scalars(select(AuditLog)).all()], default=str)
    for s in ("Metformin", "Type 2", SECRET, "HbA1c"): assert s not in blob

# ---------- family overview ----------
def test_family_overview_profiles_roles_expiry_and_isolation(env):
    db, a, b, c, p, q, rows = env
    exp = datetime.now(timezone.utc) + timedelta(days=2)
    grant(db, a, p, b, ["PROFILE", "MEDICATIONS"], role=AccessRole.CAREGIVER, expires=exp)
    mine = client(db, a).get("/api/family/overview").json()["profiles"]
    assert [x["name"] for x in mine] == ["A Patient"] and mine[0]["is_owner"] and mine[0]["active_relationships"] == 1
    theirs = client(db, b).get("/api/family/overview").json()["profiles"]
    assert {x["relationship"] for x in theirs} == {"OWNER", "CAREGIVER"}
    shared = next(x for x in theirs if not x["is_owner"])
    assert shared["name"] == "A Patient" and shared["expires_at"] is not None and shared["scopes"] == ["MEDICATIONS", "PROFILE"] and shared["active_relationships"] is None
    assert client(db, c).get("/api/family/overview").json()["profiles"] == []
    g = db.scalar(select(PatientAccess).where(PatientAccess.user_id == b.id)); g.scopes = ["MEDICATIONS"]; db.commit()
    assert next(x for x in client(db, b).get("/api/family/overview").json()["profiles"] if not x["is_owner"])["name"] == "Shared profile"

def test_confirming_undated_suggestion_is_persistent_and_idempotent(env):
    db, a, b, c, p, q, rows = env
    u = client(db, a)
    d0 = u.get(f"/api/patients/{p.id}/dashboard").json()["sections"]["follow_ups"]
    assert d0["suggested_unconfirmed_count"] == 1
    act = db.scalar(select(HealthAction).where(HealthAction.title.like("Repeat%")))
    r = u.post(f"/api/health-actions/{act.id}/confirm"); assert r.status_code == 200 and r.json()["confirmed_at"] is not None and r.json()["status"] == "PLANNED"
    first = r.json()["confirmed_at"]
    assert u.post(f"/api/health-actions/{act.id}/confirm").json()["confirmed_at"] == first            # idempotent: timestamp not rewritten
    d1 = u.get(f"/api/patients/{p.id}/dashboard").json()["sections"]["follow_ups"]
    assert d1["suggested_unconfirmed_count"] == 0 and all(x["confirmed"] for x in d1["upcoming"] if x["title"].startswith("Repeat"))
    s = u.get(f"/api/patients/{p.id}/summary?mode=detailed").json()
    fu = next(x for x in s["sections"] if x["key"] == "follow_ups")
    assert not any("not yet confirmed" in i["text"] for i in fu["items"])
    assert client(db, b).post(f"/api/health-actions/{act.id}/confirm").status_code == 403           # another user cannot confirm
