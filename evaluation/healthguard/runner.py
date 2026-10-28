"""HealthGuard AI evaluation suite runner (suite 1.0.0, dataset v1).

    python -m evaluation.healthguard.runner [--out reports/latest.json]     # exit code 1 if any release gate fails

What this measures: HealthGuard's own deterministic layers (grounding/citation/contradiction validation, medical-safety
classification, extraction validation, patient-scoped retrieval and isolation) against hand-written synthetic cases.
What it does NOT measure: the accuracy of any language model, OCR quality, or real-world clinical correctness.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).parent
DATA = HERE / "datasets" / "v1"
SUITE_VERSION = "1.0.0"
PATIENT = "11111111-1111-1111-1111-111111111111"

LIMITATIONS = [
    "Synthetic, hand-authored cases written by the team that wrote the validators: results probably flatter the system.",
    "Measures validation/safety/retrieval layers, not a language model; no live-provider extraction accuracy is measured.",
    "Small datasets: rates carry wide uncertainty; a pass is a regression bar, not evidence of clinical safety or accuracy.",
]
BLOCKED = [
    "live-provider extraction accuracy and answer quality: needs a real AI provider key and labelled documents (not available in this environment).",
]


def _load(name):
    return [json.loads(l) for l in (DATA / f"{name}.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]


def verify_manifest() -> dict:
    manifest = json.loads((DATA / "MANIFEST.json").read_text(encoding="utf-8"))
    for fname, digest in manifest["files"].items():
        actual = hashlib.sha256((DATA / fname).read_bytes()).hexdigest()
        if actual != digest:
            raise RuntimeError(f"dataset file {fname} was modified without a new dataset version (manifest mismatch)")
    return manifest


def _evidence(rows):
    out = []
    for e in rows:
        out.append({"evidence_id": e["id"], "patient_id": e.get("patient_id", PATIENT), "source_type": e.get("source_type", "HEALTH_FACT"),
                    "source_text": e["text"], "value": e["text"] if e.get("source_type", "HEALTH_FACT") == "HEALTH_FACT" else None, "details": {"fact_type": e.get("fact_type")}, "date": e.get("date"),
                    "provenance": e.get("provenance", "DOCUMENT_VERIFIED")})
    return out


def _validate(row):
    from ai.answer_provider import AnswerClaim, AnswerResult
    from domain.grounding import validate_grounded_answer
    cites = row["citations"]
    result = AnswerResult("answer", [AnswerClaim(row["claim"], list(cites))], list(cites), True, None)
    v = validate_grounded_answer(result, _evidence(row["evidence"]), expected_patient_id=PATIENT)
    return bool(v.valid and v.grounded), v.reason


def _score_validation(rows, failures, suite):
    results = []
    for r in rows:
        accepted, reason = _validate(r)
        ok = accepted == (r["expected"] == "accept")
        results.append((r, accepted, ok))
        if not ok:
            failures.append({"suite": suite, "id": r["id"], "category": r.get("category"), "expected": r["expected"], "got": "accept" if accepted else f"reject({reason})",
                             "claim": r["claim"], "critical": r.get("critical", False)})
    return results


def _rate(num, den):
    return round(num / den, 4) if den else None


def eval_grounding(failures):
    res = _score_validation(_load("grounding"), failures, "grounding")
    rej = [x for x in res if x[0]["expected"] == "reject"]; acc = [x for x in res if x[0]["expected"] == "accept"]
    per_cat = {}
    for r, accepted, ok in res:
        c = per_cat.setdefault(r["category"], {"n": 0, "wrong": 0}); c["n"] += 1; c["wrong"] += (not ok)
    return {"grounding.reject_rate_unsupported": _rate(sum(1 for _, a, _ in rej if not a), len(rej)),
            "grounding.critical_false_accept": sum(1 for r, a, _ in rej if r.get("critical") and a),
            "grounding.false_reject_supported": _rate(sum(1 for _, a, _ in acc if not a), len(acc))}, {"grounding_by_category": per_cat}


def eval_citations(failures):
    res = _score_validation(_load("citations"), failures, "citations")
    rej = [x for x in res if x[0]["expected"] == "reject"]; acc = [x for x in res if x[0]["expected"] == "accept"]
    return {"citations.reject_rate_bad_citations": _rate(sum(1 for _, a, _ in rej if not a), len(rej)),
            "citations.false_reject_valid": _rate(sum(1 for _, a, _ in acc if not a), len(acc))}, {}


def eval_contradictions(failures):
    res = _score_validation(_load("contradictions"), failures, "contradictions")
    rej = [x for x in res if x[0]["expected"] == "reject"]; acc = [x for x in res if x[0]["expected"] == "accept"]
    return {"contradictions.omission_reject_rate": _rate(sum(1 for _, a, _ in rej if not a), len(rej)),
            "contradictions.false_reject_rate": _rate(sum(1 for _, a, _ in acc if not a), len(acc))}, {}


def eval_injection(failures):
    from ai.answer_provider import AnswerProviderResponseError, _parse_answer_result
    rows = _load("injection")
    res = _score_validation(rows, failures, "injection")
    out_rej = [x for x in res if x[0]["kind"] == "output" and x[0]["expected"] == "reject"]
    ev_rej = [x for x in res if x[0]["kind"] == "evidence" and x[0]["expected"] == "reject"]
    accepted_bad = sum(1 for _, a, _ in out_rej if a)
    # Provider-contract checks: a hijacked provider must not be able to smuggle extra fields, tool calls or oversize output.
    contract = [{"answer": "x", "claims": [], "citations": [], "grounded": False, "tool_calls": [{"name": "dump_db"}]},
                {"answer": "x", "claims": [{"text": "t", "citations": ["e1"], "system": "override"}], "citations": ["e1"], "grounded": True},
                {"answer": "x" * 9000, "claims": [], "citations": [], "grounded": False},
                {"answer": "x", "claims": [{"text": "t", "citations": ["e1"]}] * 25, "citations": ["e1"], "grounded": True}]
    smuggled = 0
    for i, payload in enumerate(contract):
        try:
            _parse_answer_result(payload); smuggled += 1
            failures.append({"suite": "injection", "id": f"i-contract-{i}", "category": "provider-contract", "expected": "reject", "got": "accept", "claim": "<malformed provider payload>", "critical": True})
        except AnswerProviderResponseError:
            pass
    return {"injection.compromised_output_accept_rate": _rate(accepted_bad + smuggled, len(out_rej) + len(contract)),
            "injection.hostile_evidence_accept_rate": _rate(sum(1 for _, a, _ in ev_rej if a), len(ev_rej))}, {}


def eval_safety(failures):
    from domain.ai_safety import classify_medical_safety
    rows = _load("safety"); refuse = allow = refused_ok = over = 0
    for r in rows:
        refused = not classify_medical_safety(r["question"]).safe_for_provider
        if r["expected"] == "refuse":
            refuse += 1; refused_ok += refused
            if not refused: failures.append({"suite": "safety", "id": r["id"], "expected": "refuse", "got": "allow", "claim": r["question"], "critical": True})
        else:
            allow += 1; over += refused
            if refused: failures.append({"suite": "safety", "id": r["id"], "expected": "allow", "got": "refuse", "claim": r["question"], "critical": False})
    bad = _load("unsafe_outputs"); blocked = 0
    for u in bad:
        accepted, _ = _validate({**u, "citations": u["citations"]})
        blocked += (not accepted)
        if accepted: failures.append({"suite": "safety", "id": u["id"], "category": "unsafe-output", "expected": "reject", "got": "accept", "claim": u["claim"], "critical": True})
    return {"safety.refusal_recall": _rate(refused_ok, refuse), "safety.over_refusal_rate": _rate(over, allow),
            "safety.unsafe_output_block_rate": _rate(blocked, len(bad))}, {}


def eval_holdout(failures):
    """Generalisation check on cases written AFTER tuning. Informational (not a release gate): a large gap between these
    and the development-set metrics is the signal that the validators were fitted to the development cases."""
    res = _score_validation(_load("holdout_grounding"), failures, "holdout-grounding")
    rej = [x for x in res if x[0]["expected"] == "reject"]; acc = [x for x in res if x[0]["expected"] == "accept"]
    from domain.ai_safety import classify_medical_safety
    refuse = allow = rf = ov = 0
    for r in _load("holdout_safety"):
        refused = not classify_medical_safety(r["question"]).safe_for_provider
        if r["expected"] == "refuse":
            refuse += 1; rf += refused
            if not refused: failures.append({"suite": "holdout-safety", "id": r["id"], "expected": "refuse", "got": "allow", "claim": r["question"], "critical": True})
        else:
            allow += 1; ov += refused
            if refused: failures.append({"suite": "holdout-safety", "id": r["id"], "expected": "allow", "got": "refuse", "claim": r["question"], "critical": False})
    return {}, {"holdout": {"grounding_reject_rate_unsupported": _rate(sum(1 for _, a, _ in rej if not a), len(rej)),
                            "grounding_critical_false_accept": sum(1 for r, a, _ in rej if r.get("critical") and a),
                            "grounding_false_reject_supported": _rate(sum(1 for _, a, _ in acc if not a), len(acc)),
                            "safety_refusal_recall": _rate(rf, refuse), "safety_over_refusal_rate": _rate(ov, allow)}}


def _memory_db():
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from db.models import Base
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    @event.listens_for(e, "connect")
    def _fk(conn, _):
        conn.execute("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(e)
    return sessionmaker(bind=e)()


def eval_extraction(failures):
    from ai.document_provider import ClassificationResult, ExtractedFact
    from db.models import DocumentStatus, DocumentType, Document, DocumentTextBlock, HealthFact, PatientProfile, User
    from domain.document_analysis import analyze_document
    rows = _load("extraction"); acc_n = rej_n = acc_ok = rej_ok = 0
    for r in rows:
        db = _memory_db()
        u = User(username="u", email="u@example.com", password_hash="x", email_verified=True); db.add(u); db.flush()
        p = PatientProfile(owner_user_id=u.id, first_name="A", last_name="B"); db.add(p); db.flush()
        d = Document(patient_id=p.id, uploaded_by=u.id, filename="f.pdf", content_type="application/pdf", file_size=1, storage_key="k", checksum="0" * 64,
                     status=DocumentStatus.PROCESSING, document_type=DocumentType.LAB_REPORT); db.add(d); db.flush()
        db.add(DocumentTextBlock(document_id=d.id, page_number=1, block_index=0, source_text=r["page_text"], extraction_method="pdf_text")); db.flush()
        f = r["fact"]
        fact = ExtractedFact(f["fact_type"], f["value"], None, f["source_page"], f["source_text"], 0.9, "stub")

        class Stub:
            def classify(self, blocks): return ClassificationResult("LAB_REPORT", 1.0)
            def extract_facts(self, blocks, document_type): return [fact]
        try:
            analyze_document(db, d, Stub()); accepted = db.query(HealthFact).filter_by(document_id=d.id).count() == 1
        except ValueError:
            accepted = False
        ok = accepted == (r["expected"] == "accept")
        if r["expected"] == "accept": acc_n += 1; acc_ok += accepted
        else: rej_n += 1; rej_ok += (not accepted)
        if not ok:
            failures.append({"suite": "extraction", "id": r["id"], "category": r["category"], "expected": r["expected"], "got": "accept" if accepted else "reject",
                             "claim": f["value"] + "  <-  \"" + f["source_text"] + "\"", "critical": r["expected"] == "reject"})
        db.close()
    return {"extraction.hallucinated_reject_rate": _rate(rej_ok, rej_n), "extraction.valid_accept_rate": _rate(acc_ok, acc_n)}, {}


def _retrieval_world():
    from db.models import (DatePrecision, Document, DocumentStatus, DocumentTextBlock, DocumentType, EventSource, EventType, HealthEvent, HealthFact,
                           Medication, MedicationStatus, PatientProfile, Provenance, User)
    fx = json.loads((DATA / "retrieval_fixture.json").read_text(encoding="utf-8"))
    db = _memory_db(); owner = User(username="o", email="o@example.com", password_hash="x", email_verified=True); db.add(owner); db.flush()
    patients, key_of, owner_of = {}, {}, {}
    for name, spec in fx["patients"].items():
        p = PatientProfile(owner_user_id=owner.id, first_name=name, last_name="Test"); db.add(p); db.flush(); patients[name] = p
        docs, block_no = {}, {}
        for dk, status in spec["docs"].items():
            d = Document(patient_id=p.id, uploaded_by=owner.id, filename=f"{dk}.pdf", content_type="application/pdf", file_size=1, storage_key=f"{name}-{dk}",
                         checksum=hashlib.sha256(f"{name}{dk}".encode()).hexdigest(), status=DocumentStatus[status], document_type=DocumentType.LAB_REPORT)
            db.add(d); db.flush(); docs[dk] = d
        for key, dk, ftype, value, quote, when in spec["facts"]:
            d = docs[dk]
            db.add(DocumentTextBlock(document_id=d.id, page_number=1, block_index=block_no.__setitem__(dk, block_no.get(dk, -1) + 1) or block_no[dk], source_text=quote, extraction_method="pdf_text"))
            f = HealthFact(patient_id=p.id, document_id=d.id, fact_type=ftype, value=value, source_page=1, source_text=quote, extraction_method="stub",
                           temporal_date=date.fromisoformat(when) if when else None, temporal_precision=DatePrecision.EXACT if when else DatePrecision.UNKNOWN)
            db.add(f); db.flush(); key_of[f.id] = key; owner_of[key] = (name, status_of(docs, dk))
        for key, etype, title, when in spec["events"]:
            ev = HealthEvent(patient_id=p.id, event_type=EventType[etype], title=title, event_date=date.fromisoformat(when), date_precision=DatePrecision.EXACT,
                             created_by=owner.id, source_type=EventSource.MANUAL, provenance=Provenance.USER_REPORTED)
            db.add(ev); db.flush(); key_of[ev.id] = key; owner_of[key] = (name, "LIVE")
        for key, mname, dose, freq in spec["meds"]:
            m = Medication(patient_id=p.id, medicine_name=mname, dose=dose, frequency=freq, status=MedicationStatus.ACTIVE, created_by=owner.id)
            db.add(m); db.flush(); key_of[m.id] = key; owner_of[key] = (name, "LIVE")
    db.commit()
    return db, patients, key_of, owner_of


def status_of(docs, dk):
    return docs[dk].status.value


def eval_retrieval(failures):
    from domain.retrieval import retrieve_patient_evidence, retrieve_patient_hybrid_evidence
    db, patients, key_of, owner_of = _retrieval_world()
    rows = _load("retrieval")
    metrics, detail = {}, {}
    for mode, fn in (("hybrid", retrieve_patient_hybrid_evidence), ("deterministic", retrieve_patient_evidence)):
        hits = rr = n = leaks = gone = 0
        for r in rows:
            got = fn(db, patients[r["patient"]].id, r["query"], limit=5)
            keys = [key_of.get(g["source_id"]) for g in got]
            for g, k in zip(got, keys):
                foreign = str(g["patient_id"]) != str(patients[r["patient"]].id) or (k is not None and owner_of[k][0] != r["patient"])
                dead = k is not None and owner_of[k][1] in {"DELETED", "DELETION_PENDING"}
                leaks += foreign; gone += dead
                if foreign or dead:
                    failures.append({"suite": "retrieval", "id": r["id"], "category": f"{mode}-isolation", "expected": "no foreign/deleted evidence", "got": k or "?", "claim": r["query"], "critical": True})
            if r["relevant"]:
                n += 1; first = next((i for i, k in enumerate(keys, 1) if k in r["relevant"]), None)
                hits += first is not None; rr += (1 / first) if first else 0
                if first is None:
                    failures.append({"suite": "retrieval", "id": r["id"], "category": f"{mode}-miss", "expected": r["relevant"], "got": keys, "claim": r["query"], "critical": False})
        detail[mode] = {"hit_rate_at_5": _rate(hits, n), "mrr": _rate(rr, n), "cross_patient_leakage": leaks, "deleted_document_leakage": gone}
    h = detail["hybrid"]       # the Ask endpoint's default mode is the gated one
    metrics = {"retrieval.hit_rate_at_5": h["hit_rate_at_5"], "retrieval.mrr": h["mrr"],
               "retrieval.cross_patient_leakage": h["cross_patient_leakage"] + detail["deterministic"]["cross_patient_leakage"],
               "retrieval.deleted_document_leakage": h["deleted_document_leakage"] + detail["deterministic"]["deleted_document_leakage"]}
    return metrics, {"retrieval_by_mode": detail}


def code_fingerprint() -> str:
    root = HERE.parents[1]
    h = hashlib.sha256()
    for rel in ["domain/grounding.py", "domain/ai_safety.py", "domain/retrieval.py", "domain/document_analysis.py", "ai/answer_provider.py"]:
        h.update((root / rel).read_bytes())
    return h.hexdigest()[:16]


def run() -> dict:
    manifest = verify_manifest()
    gates_cfg = json.loads((HERE / "thresholds.json").read_text(encoding="utf-8"))["gates"]
    failures: list[dict] = []; metrics: dict = {}; detail: dict = {}
    for fn in (eval_grounding, eval_citations, eval_contradictions, eval_injection, eval_safety, eval_extraction, eval_retrieval, eval_holdout):
        m, d = fn(failures); metrics.update(m); detail.update(d)
    gates = []
    for name, cfg in gates_cfg.items():
        v = metrics.get(name)
        passed = v is not None and (v >= cfg["value"] if cfg["type"] == "min" else v <= cfg["value"])
        gates.append({"gate": name, "value": v, "type": cfg["type"], "threshold": cfg["value"], "passed": passed})
    return {"suite_version": SUITE_VERSION, "dataset_version": manifest["dataset_version"], "dataset_counts": manifest["counts"], "code_fingerprint": code_fingerprint(),
            "metrics": metrics, "gates": gates, "passed": all(g["passed"] for g in gates), "detail": detail,
            "failures": sorted(failures, key=lambda f: (f["suite"], f["id"])), "limitations": LIMITATIONS, "blocked_checks": BLOCKED}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(); ap.add_argument("--out", type=Path); args = ap.parse_args(argv)
    report = run()
    text = json.dumps(report, indent=1, sort_keys=True, default=str)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True); args.out.write_text(text, encoding="utf-8")
    for g in report["gates"]:
        print(f"{'PASS' if g['passed'] else 'FAIL'}  {g['gate']:46} {g['value']!s:8} ({g['type']} {g['threshold']})")
    print(f"\n{len(report['failures'])} case failures; suite {'PASSED' if report['passed'] else 'FAILED'}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
