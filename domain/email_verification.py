from __future__ import annotations
import os
from urllib.parse import quote, urlparse
from db.models import User


def public_base_url() -> str:
    env=os.getenv("APP_ENV","development").lower()
    base=os.getenv("HEALTHGUARD_PUBLIC_BASE_URL")
    if env != "production":
        base=base or "http://localhost:8000"
    if not base:
        raise RuntimeError("HEALTHGUARD_PUBLIC_BASE_URL must be configured in production.")
    parsed=urlparse(base)
    if parsed.scheme not in {"http","https"} or not parsed.netloc or parsed.path not in {"","/"} or parsed.query or parsed.fragment:
        raise RuntimeError("HEALTHGUARD_PUBLIC_BASE_URL must be an absolute URL.")
    host=(parsed.hostname or "").lower()
    if env == "production":
        if parsed.scheme != "https":
            raise RuntimeError("HEALTHGUARD_PUBLIC_BASE_URL must use HTTPS in production.")
        if host in {"localhost","127.0.0.1","::1"} or host.endswith(".localhost") or host.endswith(".local"):
            raise RuntimeError("HEALTHGUARD_PUBLIC_BASE_URL cannot use a development hostname in production.")
    return base.rstrip("/")

def verification_url(token:str)->str:
    return f"{public_base_url()}/api/auth/verify-email?token={quote(token,safe='')}"

def send_verification_email(user:User,token:str)->None:
    from domain.mailer import send_email, smtp_configured
    if not smtp_configured():
        if os.getenv("APP_ENV","development").lower()=="production":
            raise RuntimeError("SMTP_HOST and SMTP_FROM must be configured in production.")
        # Local development without a mail service (or dev mailbox) stays usable.
        # Never print or log the verification URL because it contains a live token.
        return
    # Same sender as reminders and alerts: one set of SMTP_* settings, TLS context, timeout and error classification.
    send_email(to=user.email, subject="Verify your HealthGuard email",
               body=f"Verify your HealthGuard account:\n\n{verification_url(token)}\n\nThis link expires in {os.getenv('HEALTHGUARD_VERIFICATION_EXPIRY_HOURS','24')} hours and can only be used once.")
