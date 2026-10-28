from __future__ import annotations
import re
from datetime import date
from typing import Any
from uuid import UUID
from sqlalchemy import select
from sqlalchemy.orm import Session
from db.models import DocumentTextBlock, HealthFact, Document, DatePrecision, Provenance

_FOLLOWUP_RE=re.compile(r"\b(follow[- ]?up|follow up|repeat|recheck|review|return|appointment|consult)\b",re.I)
_EXACT_DATE_RE=re.compile(r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b")

def suggest_from_evidence(db: Session, patient_id: UUID, *, source_type: str, source_id: UUID) -> dict[str,Any] | None:
    text=""
    provenance=Provenance.DOCUMENT_VERIFIED.value
    document_id=None; page=None
    if source_type=="HEALTH_FACT":
        row=db.scalar(select(HealthFact).where(HealthFact.id==source_id,HealthFact.patient_id==patient_id))
        if not row: return None
        text=f"{row.value} {row.source_text}"
        document_id=row.document_id; page=row.source_page; provenance=row.provenance.value
    elif source_type=="DOCUMENT_TEXT_BLOCK":
        row=db.scalar(select(DocumentTextBlock).join(Document,Document.id==DocumentTextBlock.document_id).where(DocumentTextBlock.id==source_id,Document.patient_id==patient_id,Document.status!="DELETED"))
        if not row: return None
        text=row.source_text; document_id=row.document_id; page=row.page_number
    else:
        raise ValueError("Unsupported evidence source type for follow-up extraction.")
    if not _FOLLOWUP_RE.search(text): return None
    due_date=None; precision=DatePrecision.UNKNOWN
    match=_EXACT_DATE_RE.search(text)
    if match:
        try:
            due_date=date(int(match.group(1)),int(match.group(2)),int(match.group(3))); precision=DatePrecision.EXACT
        except ValueError: pass
    title=re.sub(r"\s+"," ",text).strip()[:200]
    return {"title":title,"description":text[:2000],"action_type":"FOLLOW_UP","source":"DOCUMENT_DERIVED","provenance":provenance,"due_date":due_date,"temporal_precision":precision.value,"source_document_id":str(document_id) if document_id else None,"source_fact_id":str(source_id) if source_type=="HEALTH_FACT" else None,"source_page":page}
