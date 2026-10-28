from __future__ import annotations
from db.models import DocumentStatus

class InvalidProcessingTransition(ValueError):
    pass

_ALLOWED = {
    DocumentStatus.QUEUED: {DocumentStatus.PROCESSING},
    DocumentStatus.PROCESSING: {DocumentStatus.COMPLETED, DocumentStatus.FAILED, DocumentStatus.QUEUED},
    DocumentStatus.FAILED: {DocumentStatus.QUEUED},
    DocumentStatus.COMPLETED: set(),
}

def validate_transition(current: DocumentStatus, target: DocumentStatus) -> None:
    if target not in _ALLOWED.get(current, set()):
        raise InvalidProcessingTransition(f"Invalid processing transition: {current.value} -> {target.value}")
