"""Retrieval must stay correct and deterministic as a patient's history grows (audit finding: 125-row arbitrary window)."""
from __future__ import annotations

import random
import time
from datetime import date, timedelta

import pytest

from db.models import DatePrecision, Document, DocumentStatus, DocumentType, HealthFact, PatientProfile, User
from domain.retrieval import retrieve_patient_evidence, retrieve_patient_hybrid_evidence, retrieve_patient_semantic_evidence
from evaluation.healthguard.runner import _memory_db

ANALYTES = ["Sodium", "Potassium", "Chloride", "Calcium", "Magnesium", "Phosphate", "Urea", "Albumin", "Bilirubin", "ALT", "AST", "Ferritin", "Zinc", "Folate", "Platelets", "Glucose"]
MODES = [("deterministic", retrieve_patient_evidence), ("semantic", retrieve_patient_semantic_evidence), ("hybrid", retrieve_patient_hybrid_evidence)]


def _patient(db):
    u = User(username="u", email="u@example.com", password_hash="x", email_verified=True); db.add(u); db.flush()
    p = PatientProfile(owner_user_id=u.id, first_name="A", last_name="B"); db.add(p); db.flush()
    d = Document(patient_id=p.id, uploaded_by=u.id, filename="f.pdf", content_type="application/pdf", file_size=1, storage_key="k", checksum="0" * 64,
                 status=DocumentStatus.COMPLETED, document_type=DocumentType.LAB_REPORT); db.add(d); db.flush()
    return p, d


def _facts(p, d, texts_dates):
    return [HealthFact(patient_id=p.id, document_id=d.id, fact_type="LAB_RESULT", value=t, source_page=1, source_text=t, extraction_method="stub",
                       temporal_date=dt, temporal_precision=DatePrecision.EXACT) for t, dt in texts_dates]


def _large_history(n, needles, seed=7):
    rnd = random.Random(seed); db = _memory_db(); p, d = _patient(db)
    rows = [(f"{rnd.choice(ANALYTES)} {rnd.randint(1, 200) / 10} mmol/L", date(2020, 1, 1) + timedelta(days=i % 1500)) for i in range(n)]
    for (text, _), pos in zip(needles, rnd.sample(range(n), len(needles))):
        rows[pos] = (text, date(2020, 1, 1) + timedelta(days=pos % 1500))
    db.add_all(_facts(p, d, rows)); db.commit()
    return db, p


@pytest.mark.parametrize("n", [1500, 3000])          # 3000 exceeds the semantic scan cap on purpose
@pytest.mark.parametrize("mode,fn", MODES)
def test_exact_keyword_is_found_in_a_large_history_in_every_mode(n, mode, fn):
    needles = [("Creatinine 1.1 mg/dL", "creatinine"), ("Troponin 0.02 ng/mL", "troponin"), ("Diagnosis: Epilepsy", "epilepsy")]
    db, p = _large_history(n, needles)
    for text, query in needles:
        got = fn(db, p.id, query, limit=5)
        assert any(text.split()[0].lower().strip(":") in (g["source_text"] or "").lower() for g in got), f"{mode} missed '{query}' among {n} facts"


@pytest.mark.parametrize("mode,fn", MODES)
def test_plain_language_question_finds_the_record_term_in_a_large_history(mode, fn):
    db, p = _large_history(1500, [("Creatinine 1.1 mg/dL", "")])
    got = fn(db, p.id, "How is my kidney function?", limit=5)
    assert any("creatinine" in (g["source_text"] or "").lower() for g in got)


@pytest.mark.parametrize("mode,fn", MODES)
def test_ranking_is_deterministic_and_independent_of_random_row_ids(mode, fn):
    """Equal-scoring evidence used to be ordered by random UUIDs, so identical questions could return different orders."""
    def order(seed):
        random.seed(seed)                                              # different UUID assignment per build
        db = _memory_db(); p, d = _patient(db)
        db.add_all(_facts(p, d, [("Glucose 5.4 mmol/L", date(2024, 1, 1) + timedelta(days=i)) for i in range(12)])); db.commit()
        return [(g["date"], g["source_text"]) for g in fn(db, p.id, "glucose", limit=12)]
    a = order(1)
    assert len(a) == 12 and a == order(2) == order(3)
    assert [x[0] for x in a] == sorted([x[0] for x in a], reverse=True), "ties should prefer the most recent record"


def test_large_history_retrieval_meets_a_latency_budget():
    db, p = _large_history(3000, [("Creatinine 1.1 mg/dL", "")])
    for _, fn in MODES:
        t = time.perf_counter(); fn(db, p.id, "creatinine", limit=5); elapsed = time.perf_counter() - t
        assert elapsed < 3.0, f"retrieval took {elapsed:.2f}s on 3000 facts"
