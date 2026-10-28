from db.models import AuditLog
from domain.observability import inc, sanitize_context

def audit(db,actor,action,resource_type,resource_id=None,context=None):
    safe_context=sanitize_context(context)
    db.add(AuditLog(actor_user_id=actor.id if actor else None,action=action,resource_type=resource_type,resource_id=str(resource_id) if resource_id else None,context=safe_context))
    inc("healthguard_audit_events_total", action=action, resource_type=resource_type, outcome="recorded")
