"""Verification, reminders and alerts share one SMTP sender and one set of SMTP_* settings (Resend over SMTP in production,
the Mailpit dev mailbox locally). Real localhost SMTP capture; no external provider is contacted."""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from tests.test_deployment_readiness import _CaptureSMTP


@pytest.fixture
def relay(monkeypatch):
    server = _CaptureSMTP(); threading.Thread(target=server.serve_forever, daemon=True).start()
    for key, value in {"SMTP_HOST": "127.0.0.1", "SMTP_PORT": str(server.server_address[1]), "SMTP_FROM": "healthguard@healthguard.localhost", "SMTP_STARTTLS": "false",
                       "SMTP_USERNAME": "resend", "SMTP_PASSWORD": "re_test_key", "APP_ENV": "development", "HEALTHGUARD_PUBLIC_BASE_URL": "http://localhost:8000",
                       "HEALTHGUARD_ALERT_EMAIL_TO": "oncall@example.test, backup@example.test"}.items():
        monkeypatch.setenv(key, value)
    yield server
    server.shutdown(); server.server_close()


def _alert():
    now = datetime.now(timezone.utc)
    return SimpleNamespace(fingerprint="redis.down", source="redis", severity="CRITICAL", title="Redis is unreachable", detail=None, status="OPEN", occurrences=1, first_seen_at=now, last_seen_at=now)


def test_verification_and_alert_email_use_the_same_relay_credentials_and_sender(relay):
    from domain import alert_delivery as ad
    from domain.email_verification import send_verification_email
    send_verification_email(SimpleNamespace(email="patient@example.test"), "tok/en 1")
    ad._send_email(_alert())
    (v_from, v_to, v_body), (a_from, a_to, a_body) = relay.messages
    assert v_from == a_from == "healthguard@healthguard.localhost"
    assert v_to == ["patient@example.test"] and a_to == ["oncall@example.test", "backup@example.test"]
    assert "http://localhost:8000/api/auth/verify-email?token=tok%2Fen%201" in v_body
    assert "Redis is unreachable" in a_body or "Subject: [HealthGuard CRITICAL] Redis is unreachable" in a_body
    assert relay.logins == [("resend", "re_test_key")] * 2, "both paths must authenticate with SMTP_USERNAME/SMTP_PASSWORD"


def test_reminder_delivery_uses_the_same_sender_function(relay):
    import domain.reminders as reminders
    from domain.mailer import send_email
    assert reminders.send_email is send_email


def test_one_definition_of_email_configured_everywhere(monkeypatch):
    from domain import alert_delivery as ad
    from domain.admin_ops import config_checks
    from domain.mailer import smtp_configured
    monkeypatch.setenv("SMTP_HOST", "smtp.resend.com"); monkeypatch.delenv("SMTP_FROM", raising=False); monkeypatch.setenv("HEALTHGUARD_ALERT_EMAIL_TO", "a@example.test")
    assert smtp_configured() is False and config_checks()["smtp_configured"] is False and "email" not in ad.configured_channels()
    monkeypatch.setenv("SMTP_FROM", "a@example.test")
    assert smtp_configured() is True and config_checks()["smtp_configured"] is True and "email" in ad.configured_channels()


def test_verification_without_smtp_is_a_noop_in_development_and_an_error_in_production(monkeypatch):
    from domain.email_verification import send_verification_email
    monkeypatch.delenv("SMTP_HOST", raising=False); monkeypatch.delenv("SMTP_FROM", raising=False)
    monkeypatch.setenv("APP_ENV", "development")
    send_verification_email(SimpleNamespace(email="a@example.test"), "t")
    monkeypatch.setenv("APP_ENV", "production"); monkeypatch.setenv("HEALTHGUARD_PUBLIC_BASE_URL", "https://healthguard.example.com")
    with pytest.raises(RuntimeError, match="SMTP_HOST and SMTP_FROM"):
        send_verification_email(SimpleNamespace(email="a@example.test"), "t")


def test_api_production_validation_runs_the_shared_secret_and_smtp_checks(monkeypatch):
    from backend.main import _validate_production_config
    env = {"APP_ENV": "production", "DATABASE_URL": "postgresql+psycopg://u:p@db/healthguard", "REDIS_URL": "rediss://redis.example.com:6380/0", "STORAGE_BACKEND": "s3",
           "STORAGE_ENDPOINT": "https://s3.example.com", "STORAGE_BUCKET": "private", "STORAGE_ACCESS_KEY": "access", "STORAGE_SECRET_KEY": "secret",
           "SMTP_HOST": "smtp.resend.com", "SMTP_PORT": "587", "SMTP_USERNAME": "resend", "SMTP_PASSWORD": "re_key", "SMTP_FROM": "no-reply@mail.example.com", "SMTP_STARTTLS": "true",
           "HEALTHGUARD_LLM_PROVIDER": "openai_compatible", "HEALTHGUARD_LLM_BASE_URL": "https://llm.example.com/v1", "HEALTHGUARD_LLM_MODEL": "m", "HEALTHGUARD_LLM_API_KEY": "k",
           "HEALTHGUARD_EMBEDDING_PROVIDER": "openai_compatible", "HEALTHGUARD_EMBEDDING_BASE_URL": "https://embed.example.com/v1", "HEALTHGUARD_EMBEDDING_MODEL": "e", "HEALTHGUARD_EMBEDDING_API_KEY": "k",
           "HEALTHGUARD_AI_TIMEOUT_SECONDS": "45", "HEALTHGUARD_AI_MAX_RETRIES": "1", "HEALTHGUARD_PUBLIC_BASE_URL": "https://healthguard.example.com",
           "CORS_ALLOWED_ORIGINS": "https://healthguard.example.com", "HEALTHGUARD_METRICS_TOKEN": "m" * 40, "PROMETHEUS_MULTIPROC_DIR": "/var/run/healthguard/metrics"}
    for k, v in env.items(): monkeypatch.setenv(k, v)
    _validate_production_config()
    monkeypatch.setenv("SMTP_PORT", "465")
    with pytest.raises(RuntimeError, match="SMTP_PORT=465"):
        _validate_production_config()
    monkeypatch.setenv("SMTP_PORT", "587"); monkeypatch.setenv("HEALTHGUARD_JWT_SECRET", "replace-with-a-long-random-secret")
    with pytest.raises(RuntimeError, match="HEALTHGUARD_JWT_SECRET is still a placeholder"):
        _validate_production_config()


def test_worker_preflight_fails_closed_in_production_without_tesseract(monkeypatch):
    from jobs import extractors, worker
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setattr("utils.production_config.validate_worker_config", lambda *a, **k: None)
    def missing(): raise extractors.ExtractionError("Tesseract OCR runtime is not installed or cannot be executed.")
    monkeypatch.setattr(extractors, "check_ocr_runtime", missing)
    with pytest.raises(RuntimeError, match="OCR runtime check failed"):
        worker.preflight()
    monkeypatch.setattr(extractors, "check_ocr_runtime", lambda: "5.3.0")
    worker.preflight()


def test_worker_preflight_only_warns_in_development_and_still_runs_config_validation(monkeypatch):
    from jobs import extractors, worker
    monkeypatch.setenv("APP_ENV", "development")
    def missing(): raise extractors.ExtractionError("no tesseract")
    monkeypatch.setattr(extractors, "check_ocr_runtime", missing)
    worker.preflight()                                    # must not raise: local dev without Tesseract stays usable
    calls = []
    monkeypatch.setattr("utils.production_config.validate_worker_config", lambda *a, **k: calls.append(1))
    worker.preflight()
    assert calls == [1]


def test_run_forever_runs_preflight_before_starting_anything(monkeypatch):
    from jobs import worker
    order = []
    monkeypatch.setattr(worker, "preflight", lambda: (_ for _ in ()).throw(RuntimeError("misconfigured")))
    monkeypatch.setattr(worker, "start_metrics_server", lambda: order.append("metrics"))
    with pytest.raises(RuntimeError, match="misconfigured"):
        worker.run_forever()
    assert order == [], "a misconfigured worker must exit before opening its health port"
