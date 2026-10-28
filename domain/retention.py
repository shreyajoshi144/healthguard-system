"""Retention helpers: remove content derived from a document once the document itself is permanently deleted."""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import delete
from sqlalchemy.orm import Session

from db.models import DocumentTextBlock, HealthFact


def purge_document_derived_content(db: Session, document_id: UUID) -> dict[str, int]:
    """Delete the extracted text blocks and extracted health facts of one document.

    Call this only after the stored file has been removed (document status DELETED). Entries the user created or
    confirmed themselves (events, medications, follow-ups, measurements) are intentionally kept: they are the user's
    records, they lose their citation to the deleted file instead (FK ``SET NULL``), and the user can delete them
    individually. The function is idempotent and does not commit.
    """
    blocks = db.execute(delete(DocumentTextBlock).where(DocumentTextBlock.document_id == document_id)).rowcount or 0
    facts = db.execute(delete(HealthFact).where(HealthFact.document_id == document_id)).rowcount or 0
    return {"text_blocks": blocks, "facts": facts}
