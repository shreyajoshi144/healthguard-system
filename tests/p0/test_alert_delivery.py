"""Operational alert delivery against REAL local receivers: an aiosmtpd SMTP server and an HTTP webhook server."""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from datetime import timedelta
from email import message_from_bytes
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from aiosmtpd.controller import Controller

from db.models import OpsAlert
from domain import alert_delivery as ad
from domain.observability import prometheus
from evaluation.healthguard.runner import _memory_db
from domain.admin_ops import _aware, utcnow


class Webhook:
    def __init__(self, status=200): self.status, self.requests, self.delay = status, [], 0.0
    def start(self):
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("content-length", 0))); time.sleep(outer.delay)
                outer.requests.append((dict(self.headers), body))
                self.send_response(outer.status); self.end_headers(); self.wfile.write(b"ok")
            def log_message(self, *a): pass
        self.srv = HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}/hook"; return self
    def stop(self): self.srv.shutdown()


class Inbox:
    def __init__(self): self.messages = []
    async def handle_DATA(self, server, session, envelope):
        self.messages.append((envelope.mail_from, list(envelope.rcpt_tos), message_from_bytes(envelope.content))); return "250 OK"


@pytest.fixture
def hook(monkeypatch):
    for k in ("HEALTHGUARD_ALERT_WEBHOOK_URL", "HEALTHGUARD_ALERT_WEBHOOK_SECRET", "HEALTHGUARD_ALERT_EMAIL_TO", "SMTP_HOST"):
        monkeypatch.delenv(k, raising=False)
    w = Webhook().start(); monkeypatch.setenv("HEALTHGUARD_ALERT_WEBHOOK_URL", w.url); monkeypatch.setenv("HEALTHGUARD_ALERT_WEBHOOK_SECRET", "s3cret")
    yield w; w.stop()


@pytest.fixture
def smtp(monkeypatch):
    for k in ("HEALTHGUARD_ALERT_WEBHOOK_URL", "HEALTHGUARD_ALERT_WEBHOOK_SECRET"):
        monkeypatch.delenv(k, raising=False)
    import socket
    sk = socket.socket(); sk.bind(("127.0.0.1", 0)); port = sk.getsockname()[1]; sk.close()
    inbox = Inbox(); c = Controller(inbox, hostname="127.0.0.1", port=port); c.start()
    monkeypatch.setenv("SMTP_HOST", "127.0.0.1"); monkeypatch.setenv("SMTP_PORT", str(port)); monkeypatch.setenv("SMTP_FROM", "alerts@healthguard.test")
    monkeypatch.setenv("SMTP_STARTTLS", "false"); monkeypatch.setenv("HEALTHGUARD_ALERT_EMAIL_TO", "oncall@example.test, backup@example.test")
    yield inbox; c.stop()


def _db(): return _memory_db()


def _alert(db, fp="jobs.failed", sev="CRITICAL", title="5 document-processing job(s) failed", status="OPEN", detail=None):
    now = utcnow(); a = OpsAlert(fingerprint=fp, source="jobs", severity=sev, title=title, detail=detail, status=status, first_seen_at=now, last_seen_at=now)
    db.add(a); db.commit(); return a


def test_nothing_is_sent_or_pretended_when_no_channel_is_configured(monkeypatch):
    for k in ("HEALTHGUARD_ALERT_WEBHOOK_URL", "HEALTHGUARD_ALERT_EMAIL_TO", "SMTP_HOST"): monkeypatch.delenv(k, raising=False)
    db = _db(); a = _alert(db)
    assert ad.configured_channels() == [] and ad.deliver_pending_alerts(db)["channels"] == []
    db.refresh(a); assert a.delivery_state == "NOT_CONFIGURED" and a.delivery_attempts == 0


def test_webhook_delivery_is_signed_phi_free_and_sent_once(hook):
    db = _db(); a = _alert(db, detail="current=0017 head=0018")
    out = ad.deliver_pending_alerts(db); db.refresh(a)
    assert out["delivered"] == 1 and a.delivery_state == "DELIVERED" and a.delivery_attempts == 1
    headers, body = hook.requests[0]; headers = {k.lower(): v for k, v in headers.items()}; doc = json.loads(body)      # HTTP header names are case-insensitive
    assert doc["type"] == "healthguard.alert" and doc["severity"] == "CRITICAL" and doc["fingerprint"] == "jobs.failed" and doc["title"].startswith("5 document")
    assert set(doc) <= {"type", "version", "environment", "fingerprint", "source", "severity", "title", "detail", "status", "occurrences", "first_seen_at", "last_seen_at"}
    expected = hmac.new(b"s3cret", headers["x-healthguard-timestamp"].encode() + b"." + body, hashlib.sha256).hexdigest()
    assert headers["x-healthguard-signature"] == f"sha256={expected}"
    ad.deliver_pending_alerts(db); assert len(hook.requests) == 1, "a delivered alert must not be sent again"


def test_email_delivery_over_real_smtp(smtp):
    db = _db(); a = _alert(db, fp="redis.down", title="Redis is unreachable")
    assert ad.deliver_pending_alerts(db)["delivered"] == 1
    sender, rcpts, msg = smtp.messages[0]
    assert sender == "alerts@healthguard.test" and rcpts == ["oncall@example.test", "backup@example.test"]
    assert "[HealthGuard CRITICAL]" in msg["Subject"] and "Redis is unreachable" in msg["Subject"] and "redis.down" in msg.get_payload()


def test_failures_back_off_retry_then_become_exhausted_and_stay_visible(hook, monkeypatch):
    hook.status = 500; monkeypatch.setenv("HEALTHGUARD_ALERT_MAX_ATTEMPTS", "3")
    db = _db(); a = _alert(db); t0 = utcnow()
    assert ad.deliver_pending_alerts(db, now=t0)["failed"] == 1; db.refresh(a); assert a.delivery_state == "FAILED" and a.delivery_attempts == 1
    assert ad.deliver_pending_alerts(db, now=t0 + timedelta(seconds=5))["attempted"] == 0, "must wait for backoff"
    assert ad.deliver_pending_alerts(db, now=t0 + timedelta(seconds=40))["failed"] == 1; db.refresh(a); assert a.delivery_attempts == 2
    hook.status = 200
    assert ad.deliver_pending_alerts(db, now=t0 + timedelta(seconds=200))["delivered"] == 1; db.refresh(a); assert a.delivery_state == "DELIVERED"   # recovers when the channel does
    b = _alert(db, fp="storage.down", title="Object storage health check failed"); hook.status = 500; t1 = t0 + timedelta(hours=2)
    for i in range(6): ad.deliver_pending_alerts(db, now=t1 + timedelta(hours=i))
    db.refresh(b); assert b.delivery_state == "EXHAUSTED" and b.delivery_attempts == 3
    assert 'outcome="exhausted"' in prometheus() and 'channel="webhook"' in prometheus()


def test_only_open_alerts_at_or_above_the_minimum_severity_are_sent(hook, monkeypatch):
    monkeypatch.setenv("HEALTHGUARD_ALERT_MIN_SEVERITY", "WARNING")
    db = _db(); info = _alert(db, fp="jobs.quarantined", sev="INFO", title="1 job(s) quarantined"); gone = _alert(db, fp="x", status="RESOLVED"); ack = _alert(db, fp="y", status="ACKNOWLEDGED")
    warn = _alert(db, fp="queue.depth", sev="WARNING", title="Queue depth high")
    assert ad.deliver_pending_alerts(db)["delivered"] == 1
    assert [json.loads(b)["fingerprint"] for _, b in hook.requests] == ["queue.depth"]
    db.refresh(info); assert info.delivery_state == "BELOW_THRESHOLD"


def test_unacknowledged_critical_alerts_are_repeated_but_acknowledged_ones_are_not(hook, monkeypatch):
    monkeypatch.setenv("HEALTHGUARD_ALERT_REPEAT_HOURS", "6")
    db = _db(); a = _alert(db); t0 = utcnow(); ad.deliver_pending_alerts(db, now=t0)
    assert ad.deliver_pending_alerts(db, now=t0 + timedelta(hours=5))["attempted"] == 0
    assert ad.deliver_pending_alerts(db, now=t0 + timedelta(hours=7))["delivered"] == 1 and len(hook.requests) == 2
    db.refresh(a); a.status = "ACKNOWLEDGED"; db.commit()
    assert ad.deliver_pending_alerts(db, now=t0 + timedelta(hours=20))["attempted"] == 0


def test_concurrent_workers_never_double_send(hook):
    hook.delay = 0.3; db_url_db = _db(); a = _alert(db_url_db); Session = __import__("sqlalchemy.orm", fromlist=["sessionmaker"]).sessionmaker(bind=db_url_db.get_bind())
    results = []
    def worker():
        s = Session(); results.append(ad.deliver_pending_alerts(s)); s.close()
    threads = [threading.Thread(target=worker) for _ in range(4)]; [t.start() for t in threads]; [t.join() for t in threads]
    assert len(hook.requests) == 1, f"alert sent {len(hook.requests)} times by 4 concurrent workers"


def test_a_crashed_sender_does_not_strand_an_alert_forever(hook):
    db = _db(); a = _alert(db); a.delivery_state = "SENDING"; a.delivery_attempts = 1; a.last_delivery_at = utcnow() - timedelta(minutes=30); db.commit()
    assert ad.deliver_pending_alerts(db)["delivered"] == 1


@pytest.mark.parametrize("url,env,ok", [("ftp://x/y", "development", False), ("file:///etc/passwd", "development", False), ("http://hooks.example/x", "production", False),
                                        ("https://hooks.example/x", "production", True), ("http://127.0.0.1:9/x", "development", True), ("", "development", False)])
def test_webhook_url_is_validated(url, env, ok, monkeypatch):
    monkeypatch.setenv("APP_ENV", env); assert ad.webhook_url_ok(url) is ok


def test_webhook_redirects_are_not_followed(monkeypatch):
    target = Webhook().start()
    class R(BaseHTTPRequestHandler):
        def do_POST(self): self.rfile.read(int(self.headers.get("content-length", 0))); self.send_response(302); self.send_header("Location", target.url); self.end_headers()
        def log_message(self, *a): pass
    srv = HTTPServer(("127.0.0.1", 0), R); threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("HEALTHGUARD_ALERT_WEBHOOK_URL", f"http://127.0.0.1:{srv.server_port}/r"); monkeypatch.delenv("HEALTHGUARD_ALERT_EMAIL_TO", raising=False)
    db = _db(); a = _alert(db); out = ad.deliver_pending_alerts(db); db.refresh(a)
    srv.shutdown(); target.stop()
    assert out["failed"] == 1 and a.delivery_state == "FAILED" and not target.requests, "redirects could be used to reach internal services"


def test_one_failing_channel_does_not_block_the_other(hook, smtp, monkeypatch):
    monkeypatch.setenv("HEALTHGUARD_ALERT_WEBHOOK_URL", hook.url); hook.status = 503
    db = _db(); a = _alert(db); out = ad.deliver_pending_alerts(db); db.refresh(a)
    assert a.delivery_state == "DELIVERED" and len(smtp.messages) == 1 and out["channel_failures"] == {"webhook": 1}


def test_cycle_evaluates_real_conditions_and_delivers_without_an_admin_click(hook):
    from db.models import Document, DocumentStatus, DocumentType, PatientProfile, ProcessingJob, User
    db = _db(); u = User(username="u", email="u@example.test", password_hash="x", email_verified=True); db.add(u); db.flush()
    p = PatientProfile(owner_user_id=u.id, first_name="A", last_name="B"); db.add(p); db.flush()
    for i in range(5):
        d = Document(patient_id=p.id, uploaded_by=u.id, filename=f"{i}.pdf", content_type="application/pdf", file_size=1, storage_key=f"k{i}", checksum=str(i) * 64,
                     status=DocumentStatus.FAILED, document_type=DocumentType.UNKNOWN); db.add(d); db.flush()
        db.add(ProcessingJob(document_id=d.id, status=DocumentStatus.FAILED, attempts=1, last_error="boom"))
    db.commit()
    out = ad.run_alert_cycle(db)
    sent = {json.loads(b)["fingerprint"]: json.loads(b) for _, b in hook.requests}
    assert out["evaluation"]["firing"] >= 1 and "jobs.failed" in sent and sent["jobs.failed"]["severity"] == "CRITICAL"
    assert "boom" not in json.dumps(sent) and "u@example.test" not in json.dumps(sent)
