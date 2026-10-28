from __future__ import annotations

import os

import re
from dataclasses import asdict, dataclass, replace
from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, selectinload

from domain.clinical_terms import expand_query
from db.models import (
    DatePrecision,
    Document,
    DocumentStatus,
    DocumentTextBlock,
    HealthEvent,
    HealthFact,
    Investigation,
    InvestigationResult,
    Medication,
    MedicationEvent,
    Provenance,
)

_TOKEN_RE = re.compile(r"[a-z0-9]+(?:[./-][a-z0-9]+)*", re.I)
_DATE_RE = re.compile(r"\b(?:20\d{2})(?:[-/]\d{1,2}(?:[-/]\d{1,2})?)?\b")
_STOPWORDS = {
    "a", "an", "and", "are", "for", "from", "has", "have", "had", "in", "is", "me",
    "my", "of", "on", "the", "to", "was", "were", "what", "when", "with", "show",
    "tell", "about", "record", "records", "history", "health", "patient", "latest",
}
_FACT_ALIASES = {
    "lab": {"lab", "labs", "laboratory", "blood", "hemoglobin", "glucose", "cholesterol", "cbc"},
    "laboratory": {"lab", "labs", "laboratory", "blood", "hemoglobin", "glucose", "cholesterol", "cbc"},
    "diagnosis": {"diagnosis", "diagnoses", "condition", "conditions"},
    "medication": {"medication", "medications", "medicine", "medicines", "drug", "drugs", "tablet", "dose"},
    "investigation": {"investigation", "investigations", "test", "tests", "scan", "imaging", "result", "results"},
    "vaccination": {"vaccine", "vaccination", "vaccinations", "immunization"},
}


@dataclass(frozen=True)
class Evidence:
    patient_id: UUID
    source_type: str
    source_id: UUID
    document_id: UUID | None
    fact_id: UUID | None
    page: int | None
    source_text: str | None
    value: str | None
    details: dict[str, Any]
    date: date | None
    date_precision: str
    provenance: str
    confidence: float | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class _Candidate:
    evidence: Evidence
    searchable_text: str
    fact_type: str | None = None


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN_RE.findall(text) if t.lower() not in _STOPWORDS}


def _query_years(query: str) -> set[int]:
    return {int(x) for x in _DATE_RE.findall(query) if len(x) == 4}


def _date_relevance(query: str, value: date | None) -> float:
    if value is None:
        return 0.0
    years = _query_years(query)
    return 20.0 if value.year in years else 0.0


def _fact_type_relevance(query_tokens: set[str], fact_type: str | None) -> float:
    if not fact_type:
        return 0.0
    ft = fact_type.lower().replace("_", " ")
    ft_tokens = set(ft.split())
    for alias, terms in _FACT_ALIASES.items():
        if query_tokens & terms and (alias in ft or (alias == "lab" and "lab" in ft) or (alias == "investigation" and "investigation" in ft)):
            return 28.0
    return 0.0


def _text_score(query: str, query_tokens: set[str], searchable: str) -> float:
    text = searchable.lower()
    if not text:
        return 0.0
    score = 0.0
    normalized_query = " ".join(_TOKEN_RE.findall(query.lower()))
    if normalized_query and normalized_query in text:
        score += 60.0
    tokens = _tokens(searchable)
    overlap = len(query_tokens & tokens)
    if overlap:
        score += min(36.0, overlap * 9.0)
    return score


def _score(candidate: _Candidate, query: str, query_tokens: set[str]) -> float:
    text_score = _text_score(query, query_tokens, candidate.searchable_text)
    type_score = _fact_type_relevance(query_tokens, candidate.fact_type)
    date_score = _date_relevance(query, candidate.evidence.date)
    relevance = text_score + type_score + date_score
    if relevance <= 0:
        return 0.0
    score = relevance
    if candidate.evidence.confidence is not None:
        score += max(0.0, min(10.0, candidate.evidence.confidence * 10.0))
    if candidate.evidence.source_text:
        score += 2.0
    return score


def _fact_candidate(f: HealthFact) -> _Candidate:
    evidence = Evidence(
        patient_id=f.patient_id,
        source_type="HEALTH_FACT",
        source_id=f.id,
        document_id=f.document_id,
        fact_id=f.id,
        page=f.source_page,
        source_text=f.source_text,
        value=f.value,
        details={"fact_type": f.fact_type, "normalized_value": f.normalized_value, "extraction_method": f.extraction_method},
        date=f.temporal_date,
        date_precision=f.temporal_precision.value,
        provenance=f.provenance.value,
        confidence=f.confidence,
    )
    return _Candidate(evidence, " ".join(filter(None, [f.fact_type, f.value, f.normalized_value, f.source_text, f.source_date_text])), f.fact_type)


def _text_block_candidate(block: DocumentTextBlock, document: Document) -> _Candidate:
    evidence = Evidence(
        patient_id=document.patient_id,
        source_type="DOCUMENT_TEXT_BLOCK",
        source_id=block.id,
        document_id=document.id,
        fact_id=None,
        page=block.page_number,
        source_text=block.source_text,
        value=None,
        details={"filename": document.filename, "document_type": document.document_type.value, "extraction_method": block.extraction_method},
        date=None,
        date_precision=DatePrecision.UNKNOWN.value,
        provenance=Provenance.DOCUMENT_VERIFIED.value,
        confidence=block.confidence,
    )
    return _Candidate(evidence, " ".join([document.filename, document.document_type.value, block.source_text]), None)


def _event_candidate(e: HealthEvent) -> _Candidate:
    evidence = Evidence(
        patient_id=e.patient_id, source_type="HEALTH_EVENT", source_id=e.id, document_id=e.source_id if e.source_type.value == "DOCUMENT" else None,
        fact_id=None, page=None, source_text=None, value=e.title, details={"description": e.description, "event_type": e.event_type.value},
        date=e.event_date, date_precision=e.date_precision.value, provenance=e.provenance.value, confidence=None,
    )
    return _Candidate(evidence, " ".join(filter(None, [e.event_type.value, e.title, e.description])), e.event_type.value)


def _medication_candidate(m: Medication) -> _Candidate:
    evidence = Evidence(
        patient_id=m.patient_id, source_type="MEDICATION", source_id=m.id, document_id=m.source_id, fact_id=None, page=None,
        source_text=None, value=m.medicine_name, details={"dose": m.dose, "frequency": m.frequency, "route": m.route, "status": m.status.value, "notes": m.notes},
        date=m.start_date, date_precision=DatePrecision.EXACT.value if m.start_date else DatePrecision.UNKNOWN.value,
        provenance=m.provenance.value, confidence=None,
    )
    return _Candidate(evidence, " ".join(filter(None, [m.medicine_name, m.dose, m.frequency, m.route, m.status.value, m.notes])), "MEDICATION")


def _medication_event_candidate(e: MedicationEvent, m: Medication) -> _Candidate:
    evidence = Evidence(
        patient_id=m.patient_id, source_type="MEDICATION_EVENT", source_id=e.id, document_id=m.source_id, fact_id=None, page=None,
        source_text=None, value=m.medicine_name, details={"event_type": e.event_type.value, "dose": e.dose, "frequency": e.frequency, "notes": e.notes},
        date=e.event_date, date_precision=DatePrecision.EXACT.value if e.event_date else DatePrecision.UNKNOWN.value,
        provenance=e.provenance.value, confidence=None,
    )
    return _Candidate(evidence, " ".join(filter(None, [m.medicine_name, e.event_type.value, e.dose, e.frequency, e.notes])), "MEDICATION")


def _investigation_candidate(i: Investigation) -> _Candidate:
    d = i.performed_date or i.ordered_date
    evidence = Evidence(
        patient_id=i.patient_id, source_type="INVESTIGATION", source_id=i.id, document_id=None, fact_id=None, page=None, source_text=None,
        value=i.name, details={"status": i.status.value, "ordered_date": i.ordered_date, "performed_date": i.performed_date, "notes": i.notes},
        date=d, date_precision=DatePrecision.EXACT.value if d else DatePrecision.UNKNOWN.value, provenance=i.provenance.value, confidence=None,
    )
    return _Candidate(evidence, " ".join(filter(None, [i.name, i.status.value, i.notes])), "INVESTIGATION")


def _investigation_result_candidate(r: InvestigationResult, i: Investigation) -> _Candidate:
    evidence = Evidence(
        patient_id=i.patient_id, source_type="INVESTIGATION_RESULT", source_id=r.id, document_id=r.source_id, fact_id=None, page=None,
        source_text=None, value=r.result_summary, details={"investigation_id": str(i.id), "investigation_name": i.name, "notes": r.notes},
        date=r.result_date, date_precision=DatePrecision.EXACT.value if r.result_date else DatePrecision.UNKNOWN.value,
        provenance=i.provenance.value, confidence=None,
    )
    return _Candidate(evidence, " ".join(filter(None, [i.name, r.result_summary, r.notes])), "INVESTIGATION")


def _normalized_fact_type(fact_type: str | None) -> str | None:
    if not fact_type:
        return None
    return fact_type.strip().upper().replace(" ", "_")


# Explicit source semantics for the optional fact_type filter. A supplied fact_type
# is a source-selection contract, not merely another ranking signal.
_FACT_TYPE_SOURCES = {
    "MEDICATION": {"HEALTH_FACT", "MEDICATION", "MEDICATION_EVENT"},
    "MEDICATION_PRESCRIPTION": {"HEALTH_FACT"},
    "INVESTIGATION": {"HEALTH_FACT", "INVESTIGATION", "INVESTIGATION_RESULT"},
    "LAB_RESULT": {"HEALTH_FACT"},
    "DIAGNOSIS": {"HEALTH_FACT"},
    "CONDITION": {"HEALTH_FACT"},
    "VACCINATION": {"HEALTH_FACT"},
}


def _allowed_source_types(fact_type: str | None) -> set[str] | None:
    if fact_type is None:
        return None
    normalized = _normalized_fact_type(fact_type)
    if normalized in _FACT_TYPE_SOURCES:
        return _FACT_TYPE_SOURCES[normalized]
    # Unknown fact types are HealthFact types by definition. Never broaden an
    # explicit filter to unrelated source families just because text matches.
    return {"HEALTH_FACT"}


def _like_predicate(columns: list[Any], tokens: set[str]) -> Any | None:
    if not tokens:
        return None
    return or_(*[column.ilike(f"%{token}%") for token in tokens for column in columns])


def _date_predicate(column: Any, date_from: date | None, date_to: date | None) -> Any:
    clauses = []
    if date_from:
        clauses.append(column >= date_from)
    if date_to:
        clauses.append(column <= date_to)
    return and_(*clauses) if clauses else None


def _where(*clauses: Any) -> list[Any]:
    return [clause for clause in clauses if clause is not None]


# Deleted / deletion-pending documents must never feed Ask, search or handoffs (same rule as domain.longitudinal._live_facts).
_DEAD_DOCUMENT_STATUSES = (DocumentStatus.DELETED, DocumentStatus.DELETION_PENDING)


def _dead_document_ids(db: Session, patient_id: UUID):
    return select(Document.id).where(Document.patient_id == patient_id, Document.status.in_(_DEAD_DOCUMENT_STATUSES))


def _without_dead_document_citation(evidence: Evidence, dead_ids: set[UUID]) -> Evidence:
    """User-visible entries (events, medications, results) survive document deletion, but must not cite the deleted file."""
    if evidence.document_id is not None and evidence.document_id in dead_ids:
        return replace(evidence, document_id=None, page=None)
    return evidence


# Upper bound on rows scanned per source for semantic ranking. Keyword matches are always fetched separately in SQL, so this
# bounds only the "semantic rescue" window (most recent first); it never hides an exact keyword match.
SCAN_LIMIT = int(os.getenv("HEALTHGUARD_RETRIEVAL_SCAN_LIMIT", "2000"))


def _recent(model):
    """Deterministic, recency-first ordering for candidate queries (a LIMIT without ORDER BY returns an arbitrary slice)."""
    for name in ("temporal_date", "event_date", "start_date", "performed_date", "result_date", "created_at"):
        col = getattr(model, name, None)
        if col is not None:
            return (col.desc().nulls_last(), model.id)
    return (model.id,)


def _ordinal(d) -> int:
    if d is None or d == "":
        return 0
    if hasattr(d, "toordinal"):
        return d.toordinal()
    try:
        return date.fromisoformat(str(d)[:10]).toordinal()
    except ValueError:
        return 0


def _tie_item(item: dict) -> tuple:
    """Tie-break equal scores by recency, then content, then id: never by a random UUID alone."""
    return (-_ordinal(item.get("date")), str(item.get("value") or item.get("source_text") or "").lower(), str(item.get("source_id")))


def _tie_ev(ev) -> tuple:
    return (-_ordinal(ev.date), str(ev.value or ev.source_text or "").lower(), str(ev.source_id))


def _scan_candidates(db, patient_id, query, *, date_from, date_to, fact_type, limit, allowed_scopes):
    """Union of (a) every keyword match, fetched in SQL, and (b) the most recent SCAN_LIMIT rows for semantic rescue."""
    kw = dict(date_from=date_from, date_to=date_to, fact_type=fact_type, allowed_scopes=allowed_scopes)
    lexical = retrieve_patient_evidence(db, patient_id, query, limit=max(limit * 5, 25), _apply_text_filter=True, **kw)
    scan = retrieve_patient_evidence(db, patient_id, query, limit=SCAN_LIMIT, _apply_text_filter=False, **kw)
    seen, out = set(), []
    for item in lexical + scan:
        key = (item["source_type"], str(item["source_id"]))
        if key not in seen:
            seen.add(key); out.append(item)
    return out


def retrieve_patient_evidence(
    db: Session,
    patient_id: UUID,
    query: str,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    fact_type: str | None = None,
    limit: int = 20,
    _apply_text_filter: bool = True,
    allowed_scopes: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Retrieve ranked evidence for one already-authorized patient.

    Authorization is intentionally performed by the API boundary before this
    function runs. Every source query is patient-scoped, and obvious date,
    fact-type, text, and candidate-limit constraints are pushed into SQL.

    ``fact_type`` is an explicit source-selection filter. LAB_RESULT, for
    example, selects typed LAB_RESULT HealthFacts only; medication and
    investigation records cannot enter merely because their text matches.
    Medication and investigation source families are included only where
    explicitly defined above.
    """
    query = expand_query(query.strip())
    if not query:
        return []
    if date_from and date_to and date_to < date_from:
        raise ValueError("date_to cannot precede date_from")
    limit = max(1, min(limit, 100))
    candidate_limit = min(SCAN_LIMIT, max(limit * 5, 25))
    query_tokens = _tokens(query)
    normalized_fact_type = _normalized_fact_type(fact_type)
    allowed_sources = _allowed_source_types(normalized_fact_type)
    candidates: list[_Candidate] = []

    source_scopes = {
        "HEALTH_FACT": "DOCUMENTS",
        "DOCUMENT_TEXT_BLOCK": "DOCUMENTS",
        "HEALTH_EVENT": "TIMELINE",
        "MEDICATION": "MEDICATIONS",
        "MEDICATION_EVENT": "MEDICATIONS",
        "INVESTIGATION": "INVESTIGATIONS",
        "INVESTIGATION_RESULT": "INVESTIGATIONS",
    }
    def include(source_type: str) -> bool:
        return (allowed_sources is None or source_type in allowed_sources) and (allowed_scopes is None or source_scopes.get(source_type) in allowed_scopes)

    # HealthFact: patient/date/fact_type/text are all SQL-side filters.
    if include("HEALTH_FACT"):
        fact_clauses = [HealthFact.patient_id == patient_id, or_(HealthFact.document_id.is_(None), HealthFact.document_id.not_in(_dead_document_ids(db, patient_id)))]
        if normalized_fact_type:
            fact_clauses.append(HealthFact.fact_type == normalized_fact_type)
        if (date_clause := _date_predicate(HealthFact.temporal_date, date_from, date_to)) is not None:
            fact_clauses.append(date_clause)
        if _apply_text_filter and (text_clause := _like_predicate(
            [HealthFact.fact_type, HealthFact.value, HealthFact.normalized_value, HealthFact.source_text, HealthFact.source_date_text],
            query_tokens,
        )) is not None:
            fact_clauses.append(text_clause)
        facts = db.scalars(select(HealthFact).where(*fact_clauses).order_by(*_recent(HealthFact)).limit(candidate_limit)).all()
        candidates.extend(_fact_candidate(f) for f in facts)

    # Raw document text has no intrinsic clinical date or fact_type. Therefore
    # it is eligible only when no explicit date/fact_type filter was supplied.
    if include("DOCUMENT_TEXT_BLOCK") and not normalized_fact_type and not date_from and not date_to:
        block_clauses = [Document.patient_id == patient_id, Document.status.not_in(_DEAD_DOCUMENT_STATUSES)]
        if _apply_text_filter and (text_clause := _like_predicate([Document.filename, Document.document_type, DocumentTextBlock.source_text], query_tokens)) is not None:
            block_clauses.append(text_clause)
        blocks = db.scalars(
            select(DocumentTextBlock)
            .join(Document, Document.id == DocumentTextBlock.document_id)
            .where(*block_clauses)
            .order_by(*_recent(DocumentTextBlock)).limit(candidate_limit)
        ).all()
        # Avoid loading all patient documents just to resolve metadata.
        block_document_ids = {b.document_id for b in blocks}
        documents = db.scalars(select(Document).where(Document.id.in_(block_document_ids))).all() if block_document_ids else []
        by_id = {d.id: d for d in documents}
        candidates.extend(_text_block_candidate(b, by_id[b.document_id]) for b in blocks if b.document_id in by_id)

    if include("HEALTH_EVENT"):
        event_clauses = [HealthEvent.patient_id == patient_id]
        if (date_clause := _date_predicate(HealthEvent.event_date, date_from, date_to)) is not None:
            event_clauses.append(date_clause)
        if _apply_text_filter and (text_clause := _like_predicate([HealthEvent.event_type, HealthEvent.title, HealthEvent.description], query_tokens)) is not None:
            event_clauses.append(text_clause)
        events = db.scalars(select(HealthEvent).where(*event_clauses).order_by(*_recent(HealthEvent)).limit(candidate_limit)).all()
        candidates.extend(_event_candidate(e) for e in events)

    if include("MEDICATION"):
        medication_clauses = [Medication.patient_id == patient_id]
        if date_from:
            medication_clauses.append(Medication.start_date >= date_from)
        if date_to:
            medication_clauses.append(Medication.start_date <= date_to)
        if _apply_text_filter and (text_clause := _like_predicate([Medication.medicine_name, Medication.dose, Medication.frequency, Medication.route, Medication.status, Medication.notes], query_tokens)) is not None:
            medication_clauses.append(text_clause)
        meds = db.scalars(select(Medication).where(*medication_clauses).order_by(*_recent(Medication)).limit(candidate_limit)).all()
        candidates.extend(_medication_candidate(m) for m in meds)

    if include("MEDICATION_EVENT"):
        medication_event_clauses = [Medication.patient_id == patient_id]
        if date_from:
            medication_event_clauses.append(MedicationEvent.event_date >= date_from)
        if date_to:
            medication_event_clauses.append(MedicationEvent.event_date <= date_to)
        if _apply_text_filter and (text_clause := _like_predicate([Medication.medicine_name, MedicationEvent.event_type, MedicationEvent.dose, MedicationEvent.frequency, MedicationEvent.notes], query_tokens)) is not None:
            medication_event_clauses.append(text_clause)
        medication_events = db.execute(
            select(MedicationEvent, Medication).join(Medication, Medication.id == MedicationEvent.medication_id).where(*medication_event_clauses).order_by(*_recent(MedicationEvent)).limit(candidate_limit)
        ).all()
        candidates.extend(_medication_event_candidate(event, medication) for event, medication in medication_events)

    if include("INVESTIGATION"):
        investigation_clauses = [Investigation.patient_id == patient_id]
        investigation_date = or_(Investigation.performed_date, Investigation.ordered_date)
        if date_from:
            investigation_clauses.append(investigation_date >= date_from)
        if date_to:
            investigation_clauses.append(investigation_date <= date_to)
        if _apply_text_filter and (text_clause := _like_predicate([Investigation.name, Investigation.status, Investigation.notes], query_tokens)) is not None:
            investigation_clauses.append(text_clause)
        investigations = db.scalars(select(Investigation).where(*investigation_clauses).order_by(*_recent(Investigation)).limit(candidate_limit)).all()
        candidates.extend(_investigation_candidate(i) for i in investigations)

    if include("INVESTIGATION_RESULT"):
        result_clauses = [Investigation.patient_id == patient_id]
        if date_from:
            result_clauses.append(InvestigationResult.result_date >= date_from)
        if date_to:
            result_clauses.append(InvestigationResult.result_date <= date_to)
        if _apply_text_filter and (text_clause := _like_predicate([Investigation.name, InvestigationResult.result_summary, InvestigationResult.notes], query_tokens)) is not None:
            result_clauses.append(text_clause)
        investigation_results = db.execute(
            select(InvestigationResult, Investigation)
            .join(Investigation, Investigation.id == InvestigationResult.investigation_id)
            .where(*result_clauses)
            .order_by(*_recent(InvestigationResult)).limit(candidate_limit)
        ).all()
        candidates.extend(_investigation_result_candidate(result, investigation) for result, investigation in investigation_results)

    dead_ids = set(db.scalars(_dead_document_ids(db, patient_id)).all())
    if dead_ids:
        candidates = [replace(c, evidence=_without_dead_document_citation(c.evidence, dead_ids)) for c in candidates]

    filtered: list[tuple[float, _Candidate]] = []
    for c in candidates:
        # Keep Python-side date checks as a correctness guard for sources whose
        # effective date is derived from a related row.
        d = c.evidence.date
        if date_from and (d is None or d < date_from):
            continue
        if date_to and (d is None or d > date_to):
            continue
        score = _score(c, query, query_tokens)
        if score <= 0 and _apply_text_filter:
            continue
        filtered.append((score, c))

    filtered.sort(key=lambda pair: (-pair[0], -((pair[1].evidence.confidence or 0.0)), pair[1].evidence.source_type, *_tie_ev(pair[1].evidence)))
    return [c.evidence.as_dict() | {"score": round(score, 6)} for score, c in filtered[:limit]]


def retrieve_patient_semantic_evidence(
    db: Session, patient_id: UUID, query: str, *, date_from: date | None = None,
    date_to: date | None = None, fact_type: str | None = None, limit: int = 20,
    embedding_provider=None, allowed_scopes: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Semantic ranking over an already patient-scoped candidate set.

    Structured patient/date/fact-type constraints are applied before embedding.
    Raw vectors are never returned.
    """
    from ai.embedding_provider import cosine_similarity, get_embedding_provider
    query = expand_query(query.strip())
    if not query:
        return []
    candidates = _scan_candidates(db, patient_id, query, date_from=date_from, date_to=date_to, fact_type=fact_type, limit=max(1, limit), allowed_scopes=allowed_scopes)
    if not candidates:
        return []
    provider = embedding_provider or get_embedding_provider()
    vectors = provider.embed([query] + [
        " ".join(filter(None, [str(x.get("value") or ""), str(x.get("source_text") or ""), str(x.get("details") or "")]))
        for x in candidates
    ])
    qv = vectors[0]
    ranked = []
    for item, vec in zip(candidates, vectors[1:]):
        similarity = max(0.0, cosine_similarity(qv, vec))
        ranked.append((similarity, item))
    ranked.sort(key=lambda x: (-x[0], -float(x[1].get("confidence") or 0.0), x[1].get("source_type", ""), *_tie_item(x[1])))
    return [item | {"score": round(similarity, 6), "semantic_similarity": round(similarity, 6), "retrieval_mode": "semantic"}
            for similarity, item in ranked[:max(1, min(limit, 100))]]


def retrieve_patient_hybrid_evidence(
    db: Session, patient_id: UUID, query: str, *, date_from: date | None = None,
    date_to: date | None = None, fact_type: str | None = None, limit: int = 20,
    embedding_provider=None, allowed_scopes: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Patient-scoped hybrid retrieval. Structured predicates run in SQL; semantic ranking runs only on those patient-owned candidates."""
    from ai.embedding_provider import cosine_similarity, get_embedding_provider
    query=expand_query(query.strip())
    if not query: return []
    candidates=_scan_candidates(db,patient_id,query,date_from=date_from,date_to=date_to,fact_type=fact_type,limit=max(1,limit),allowed_scopes=allowed_scopes)
    if not candidates: return []
    provider=embedding_provider or get_embedding_provider()
    texts=[query]+[" ".join(filter(None,[str(x.get('value') or ''),str(x.get('source_text') or ''),str(x.get('details') or '')])) for x in candidates]
    vectors=provider.embed(texts)
    qv=vectors[0]
    rescored=[]
    for item,vec in zip(candidates,vectors[1:]):
        semantic=max(0.0,cosine_similarity(qv,vec))
        deterministic=float(item.get('score') or 0.0)
        confidence=float(item.get('confidence') or 0.0)
        provenance_bonus={'DOCUMENT_VERIFIED':4.0,'USER_REPORTED':2.0,'FAMILY_REPORTED':1.5,'SYSTEM_DERIVED':1.0}.get(str(item.get('provenance')),0.0)
        temporal=8.0 if date_from or date_to else 0.0
        # Structured/keyword relevance is the primary signal. Semantic similarity
        # is a bounded bonus and can rescue a semantically relevant candidate that
        # has no lexical overlap, but it cannot overpower a strong deterministic
        # match. This prevents generic semantic neighbors from displacing exact
        # patient-record matches.
        semantic_bonus=max(0.0, semantic - 0.55) * 18.0
        deterministic_strength=min(1.0, deterministic / 60.0)
        gate=0.35 + 0.65 * (1.0 - deterministic_strength)
        hybrid=deterministic + semantic_bonus * gate + confidence*5.0 + provenance_bonus + temporal
        rescored.append((hybrid,semantic,item))
    rescored.sort(key=lambda x:(-x[0],-x[1],-float(x[2].get('confidence') or 0),x[2].get('source_type',''),*_tie_item(x[2])))
    return [item|{'score':round(score,6),'semantic_similarity':round(sim,6),'retrieval_mode':'hybrid'} for score,sim,item in rescored[:max(1,min(limit,100))]]
