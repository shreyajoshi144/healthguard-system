from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ai.document_provider import DocumentAnalysisProvider, ExtractedFact
from db.models import DatePrecision, Document, DocumentTextBlock, DocumentType, HealthFact, Provenance
from domain.observability import inc

import re

_LABEL_PREFIX = re.compile(r"^\s*(?:diagnosis|dx|allergy|medication|prescription|lab(?:\s+result)?|result|symptom|family\s+history|procedure|vital)\s*:\s*", re.I)


def fact_value_supported_by_source(value: str, source_text: str) -> bool:
    """An extracted value must be stated by the quote it cites (same numbers, units, polarity, subject). Checking only that the
    quote exists on the page would let a model attach an invented value to a real sentence."""
    from domain.grounding import _claim_supported
    return _claim_supported(_LABEL_PREFIX.sub("", value or ""), source_text or "")


def _blocks(db: Session, document_id):
    rows = db.scalars(select(DocumentTextBlock).where(DocumentTextBlock.document_id == document_id).order_by(DocumentTextBlock.page_number, DocumentTextBlock.block_index)).all()
    return [{"id": str(b.id), "page_number": b.page_number, "source_text": b.source_text, "confidence": b.confidence} for b in rows]


def _safe_type(value: str) -> DocumentType:
    try:
        return DocumentType(value.upper())
    except ValueError:
        return DocumentType.UNKNOWN


def analyze_document(db: Session, document: Document, provider: DocumentAnalysisProvider) -> list[HealthFact]:
    blocks = _blocks(db, document.id)
    if not blocks:
        raise ValueError("Document has no extracted text evidence.")
    classification = provider.classify(blocks)
    document.document_type = _safe_type(classification.document_type)
    facts = provider.extract_facts(blocks, document.document_type.value)

    # Server-controlled provenance: clients have no write path to HealthFact.
    db.execute(delete(HealthFact).where(HealthFact.document_id == document.id))
    result: list[HealthFact] = []
    seen: set[tuple] = set()
    for fact in facts:
        key = (fact.fact_type, fact.value, fact.normalized_value, fact.source_page, fact.source_text)
        if key in seen:
            continue
        seen.add(key)
        block = next((b for b in blocks if b["page_number"] == fact.source_page and fact.source_text in b["source_text"]), None)
        if block is None:
            raise ValueError("Fact evidence could not be matched to a source block.")
        if not fact_value_supported_by_source(fact.value, fact.source_text):
            # Never persisted, but one unsupported fact must not fail an otherwise valid document.
            inc("healthguard_extraction_facts_rejected_total", reason="value_not_supported_by_source")
            continue
        temporal_date = fact.temporal_date
        temporal_precision = fact.temporal_precision
        if temporal_date is None and temporal_precision is not DatePrecision.UNKNOWN:
            raise ValueError("Temporal precision requires an explicit date.")
        if temporal_date is not None and temporal_precision is DatePrecision.UNKNOWN:
            raise ValueError("An explicit fact date requires temporal precision.")
        if temporal_precision is DatePrecision.YEAR and temporal_date is not None and (temporal_date.month != 1 or temporal_date.day != 1):
            raise ValueError("YEAR precision requires January 1 as the stored date marker.")
        row = HealthFact(
            patient_id=document.patient_id,
            document_id=document.id,
            fact_type=fact.fact_type,
            value=fact.value,
            normalized_value=fact.normalized_value,
            temporal_date=temporal_date,
            temporal_precision=temporal_precision,
            source_date_text=fact.source_date_text,
            source_page=fact.source_page,
            source_text=fact.source_text,
            confidence=fact.confidence,
            extraction_method=fact.extraction_method,
            provenance=Provenance.DOCUMENT_VERIFIED,
        )
        db.add(row)
        result.append(row)
    return result
