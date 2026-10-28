"""The single SMTP sender used by account verification, follow-up reminders and operational alerts.

All three paths read the same ``SMTP_*`` settings and go through :func:`send_email`, so a provider (Resend over SMTP, a
dev mailbox such as Mailpit, or any other relay) behaves identically everywhere. Failures are classified without PHI.
"""
from __future__ import annotations
import os, smtplib, socket, ssl
from collections.abc import Sequence
from email.message import EmailMessage


class EmailDeliveryError(Exception):
    """`code` is a short, PHI-free reason; `permanent` means retrying cannot help."""
    def __init__(self, code: str, permanent: bool):
        super().__init__(code); self.code, self.permanent = code, permanent


def smtp_configured() -> bool:
    return bool(os.getenv("SMTP_HOST") and os.getenv("SMTP_FROM"))


def send_email(*, to: str | Sequence[str], subject: str, body: str, message_id: str | None = None) -> None:
    host, sender = os.getenv("SMTP_HOST"), os.getenv("SMTP_FROM")
    if not host or not sender:
        raise EmailDeliveryError("smtp_not_configured", permanent=True)
    recipients = [to] if isinstance(to, str) else [r for r in to if r]
    if not recipients:
        raise EmailDeliveryError("no_recipients", permanent=True)
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, sender, ", ".join(recipients)
    if message_id: msg["Message-ID"] = message_id
    msg.set_content(body)
    try:
        with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587")), timeout=float(os.getenv("SMTP_TIMEOUT_SECONDS", "15"))) as server:
            if os.getenv("SMTP_STARTTLS", "true").lower() == "true": server.starttls(context=ssl.create_default_context())
            if os.getenv("SMTP_USERNAME"): server.login(os.getenv("SMTP_USERNAME"), os.getenv("SMTP_PASSWORD", ""))
            server.send_message(msg, to_addrs=recipients)
    except smtplib.SMTPRecipientsRefused as exc:
        raise EmailDeliveryError("recipient_refused", permanent=True) from exc
    except smtplib.SMTPAuthenticationError as exc:
        raise EmailDeliveryError("smtp_auth_failed", permanent=False) from exc
    except smtplib.SMTPResponseException as exc:
        raise EmailDeliveryError(f"smtp_{exc.smtp_code}", permanent=500 <= exc.smtp_code < 600) from exc
    except (smtplib.SMTPException, socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
        raise EmailDeliveryError("smtp_unavailable", permanent=False) from exc
