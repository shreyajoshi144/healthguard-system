"""Durable, PHI-free security signals (401/403/429 on API routes).

Only the route template, method, status, hashed client address and (when known)
the acting account id are stored. No query strings, bodies, headers or tokens.
Recording is best-effort: a failure here must never affect the user's request.
"""
from __future__ import annotations
import hashlib, logging, os, re
from datetime import datetime, timedelta, timezone
from sqlalchemy import delete
from db.models import SecurityEvent
from db.session import SessionLocal

logger = logging.getLogger(__name__)
_session_factory = SessionLocal
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
EVENT_FOR_STATUS = {401: "AUTH_FAILURE", 403: "AUTHORIZATION_FAILURE", 428: "ADMIN_STEP_UP_REQUIRED", 429: "RATE_LIMITED"}
RETENTION_DAYS = int(os.getenv("HEALTHGUARD_SECURITY_EVENT_RETENTION_DAYS", "30"))

def set_session_factory(factory):
    global _session_factory
    _session_factory = factory

def client_hash(ip: str | None) -> str:
    salt = os.getenv("HEALTHGUARD_SECURITY_EVENT_SALT", "healthguard")
    return hashlib.sha256(f"{salt}:{ip or 'unknown'}".encode()).hexdigest()[:16]

def route_label(request) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", None) or _UUID.sub("{id}", request.url.path)
    return path[:120]

def record(*, status: int, route: str, method: str, ip: str | None, actor_user_id=None, request_id: str | None = None, event_type: str | None = None) -> None:
    etype = event_type or EVENT_FOR_STATUS.get(status)
    if not etype:
        return
    db = _session_factory()
    try:
        db.add(SecurityEvent(event_type=etype, route=route, method=method[:8], status_code=status, actor_user_id=actor_user_id, client_hash=client_hash(ip), request_id=(request_id or None)))
        db.commit()
    except Exception as exc:                      # never break the request path
        db.rollback()
        logger.warning("security_event_record_failed error_type=%s", type(exc).__name__)
    finally:
        db.close()

def prune(db) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    res = db.execute(delete(SecurityEvent).where(SecurityEvent.occurred_at < cutoff))
    return res.rowcount or 0
