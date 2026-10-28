"""Production configuration checks shared by the API (``backend.main``) and the worker (``jobs.worker``).

Standard library only, so it can run before any application import. Every check is a no-op unless ``APP_ENV=production``.
Error messages name the variable, never its value.
"""
from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from urllib.parse import urlparse

# Values shipped in .env.example / docs that must never reach production.
PLACEHOLDER_MARKERS = ("replace-with", "replace_with", "changeme", "change-me", "change_me")
SECRET_VARIABLES = (
    "HEALTHGUARD_JWT_SECRET", "HEALTHGUARD_METRICS_TOKEN", "HEALTHGUARD_ADMIN_TOTP_KEY",
    "STORAGE_ACCESS_KEY", "STORAGE_SECRET_KEY", "SMTP_PASSWORD", "HEALTHGUARD_LLM_API_KEY", "HEALTHGUARD_EMBEDDING_API_KEY",
)
DEV_MAIL_HOSTS = {"localhost", "127.0.0.1", "::1", "mailpit", "mailhog"}
_TRUE = {"true", "1", "yes", "on"}


def is_production(env: Mapping[str, str] | None = None) -> bool:
    return (env if env is not None else os.environ).get("APP_ENV", "development").lower() == "production"


def validate_secrets(env: Mapping[str, str] | None = None) -> None:
    """Reject the placeholder secrets from .env.example (and a too-short admin TOTP key, which would otherwise silently disable /admin)."""
    env = env if env is not None else os.environ
    if not is_production(env):
        return
    for name in SECRET_VARIABLES:
        value = (env.get(name) or "").strip().lower()
        if value and any(marker in value for marker in PLACEHOLDER_MARKERS):
            raise RuntimeError(f"{name} is still a placeholder value; set a real secret for production.")
    totp = env.get("HEALTHGUARD_ADMIN_TOTP_KEY")
    if totp and len(totp) < 32:
        raise RuntimeError("HEALTHGUARD_ADMIN_TOTP_KEY must be at least 32 characters when set.")


def validate_smtp(env: Mapping[str, str] | None = None) -> None:
    """One SMTP configuration serves verification, reminders and alerts; check it once, identically, in API and worker.

    Works for any STARTTLS relay (Resend over SMTP: smtp.resend.com:587, user ``resend``, password = API key).
    """
    env = env if env is not None else os.environ
    if not is_production(env):
        return
    host, sender = (env.get("SMTP_HOST") or "").strip(), (env.get("SMTP_FROM") or "").strip()
    if not host or not sender:
        raise RuntimeError("SMTP_HOST and SMTP_FROM must be configured in production.")
    if host.lower() in DEV_MAIL_HOSTS or host.lower().endswith((".localhost", ".local")):
        raise RuntimeError("SMTP_HOST cannot be a development mailbox in production.")
    if "@" not in sender:
        raise RuntimeError("SMTP_FROM must be an email address.")
    try:
        port = int(env.get("SMTP_PORT", "587"))
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError as exc:
        raise RuntimeError("SMTP_PORT must be a valid port number.") from exc
    if port == 465:
        raise RuntimeError("SMTP_PORT=465 needs implicit TLS, which HealthGuard's mailer does not use; use port 587 with SMTP_STARTTLS=true.")
    if (env.get("SMTP_STARTTLS", "true").lower()) not in _TRUE:
        raise RuntimeError("SMTP_STARTTLS must be enabled in production.")
    if bool(env.get("SMTP_USERNAME")) != bool(env.get("SMTP_PASSWORD")):
        raise RuntimeError("SMTP_USERNAME and SMTP_PASSWORD must be set together.")


def validate_worker_config(env: Mapping[str, str] | None = None, public_base_url: Callable[[], str] | None = None) -> None:
    """Fail the worker at startup (instead of at the first reminder) when what it needs to send email is missing."""
    env = env if env is not None else os.environ
    if not is_production(env):
        return
    validate_secrets(env)
    if (env.get("HEALTHGUARD_REMINDERS_ENABLED", "true").lower()) == "true":
        validate_smtp(env)                       # reminders are sent by the worker, so it needs the same SMTP_* as the API
        if public_base_url is None:
            from domain.email_verification import public_base_url as _pbu
            public_base_url = _pbu
        public_base_url()                        # reminder links; raises unless an HTTPS public URL is configured
    elif env.get("HEALTHGUARD_ALERT_EMAIL_TO", "").strip():
        validate_smtp(env)
    token = env.get("HEALTHGUARD_METRICS_TOKEN") or ""
    if len(token) < 32:
        raise RuntimeError("HEALTHGUARD_METRICS_TOKEN must be at least 32 characters in production.")
    if urlparse(env.get("REDIS_URL", "")).scheme not in {"redis", "rediss"}:
        raise RuntimeError("Production REDIS_URL must be a valid redis:// or rediss:// URL.")
