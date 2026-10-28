"""Explicit privacy consents.

AI processing sends text from a patient's record to an external model provider, so it needs the record OWNER's explicit,
current consent. When no external provider is configured nothing leaves the system and no consent is required.
Consent is versioned: changing a purpose's wording (bumping ``version``) invalidates earlier consents until re-granted.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import Consent, PatientProfile, User

PURPOSES: dict[str, dict] = {
    "AI_PROCESSING": {
        "version": "1",
        "title": "AI processing of my health records",
        "description": (
            "Allow HealthGuard to send text from my uploaded documents, and my questions with the record excerpts they need, to the external AI "
            "provider configured for this service so it can extract facts and draft answers. Answers are checked against my record and are not "
            "medical advice. I can withdraw this at any time; withdrawing stops new AI processing but does not delete existing records."
        ),
    },
}


class ConsentRequired(Exception):
    def __init__(self, purpose: str): self.purpose = purpose


def external_ai_configured() -> bool:
    return bool(os.getenv("HEALTHGUARD_LLM_API_KEY", "").strip())


def has_consent(db: Session, user_id, purpose: str) -> bool:
    row = db.scalar(select(Consent).where(Consent.user_id == user_id, Consent.purpose == purpose))
    return bool(row and row.granted and row.policy_version == PURPOSES[purpose]["version"])


def list_consents(db: Session, user: User) -> list[dict]:
    rows = {c.purpose: c for c in db.scalars(select(Consent).where(Consent.user_id == user.id)).all()}
    out = []
    for purpose, spec in PURPOSES.items():
        row = rows.get(purpose)
        out.append({"purpose": purpose, "title": spec["title"], "description": spec["description"], "policy_version": spec["version"],
                    "granted": has_consent(db, user.id, purpose), "decided_at": row.decided_at if row else None,
                    "needs_reconsent": bool(row and row.granted and row.policy_version != spec["version"]),
                    "required_now": purpose == "AI_PROCESSING" and external_ai_configured()})
    return out


def set_consent(db: Session, user: User, purpose: str, granted: bool) -> Consent:
    if purpose not in PURPOSES:
        raise HTTPException(404, "Unknown consent purpose.")
    row = db.scalar(select(Consent).where(Consent.user_id == user.id, Consent.purpose == purpose))
    if row is None:
        row = Consent(user_id=user.id, purpose=purpose, policy_version=PURPOSES[purpose]["version"], granted=granted)
        db.add(row)
    row.policy_version = PURPOSES[purpose]["version"]; row.granted = granted; row.decided_at = datetime.now(timezone.utc)
    db.flush()
    return row


def owner_consented(db: Session, patient: PatientProfile, purpose: str = "AI_PROCESSING") -> bool:
    """True when the purpose does not apply (nothing leaves the system) or the record owner has consented."""
    if purpose == "AI_PROCESSING" and not external_ai_configured():
        return True
    return has_consent(db, patient.owner_user_id, purpose)


def require_ai_consent(db: Session, patient: PatientProfile) -> None:
    if not owner_consented(db, patient):
        raise HTTPException(403, detail={"code": "CONSENT_REQUIRED", "purpose": "AI_PROCESSING",
                                         "message": "The record owner must allow AI processing in Settings > Privacy before this can be used."})
