from __future__ import annotations
import json, logging
from fastapi.testclient import TestClient


def test_request_observability_has_correlation_and_structured_fields(caplog):
    from backend.main import app
    from utils.logger import _JSONFormatter
    with caplog.at_level(logging.INFO):
        response=TestClient(app).get('/api/health', headers={'X-Request-ID':'req-test-123','X-Correlation-ID':'corr-test-456'})
    assert response.status_code==200
    assert response.headers['X-Request-ID']=='req-test-123'
    assert response.headers['X-Correlation-ID']=='corr-test-456'
    record=next(r for r in caplog.records if r.name=='backend.main' and getattr(r,'route',None)=='/api/health')
    payload=json.loads(_JSONFormatter().format(record))
    assert payload['request_id']=='req-test-123'
    assert payload['correlation_id']=='corr-test-456'
    assert payload['method']=='GET' and payload['status']==200
    assert 'latency_ms' in payload


def test_metrics_are_prometheus_compatible_and_have_no_sensitive_values():
    from domain.observability import inc, prometheus
    inc('healthguard_test_metric', password='should-not-be-secret-content', safe='ok')
    text=prometheus()
    assert 'healthguard_test_metric' in text
    assert 'should-not-be-secret-content' not in text
    assert 'password' not in text.lower()
    assert 'should-not-be-secret-content' not in text


def test_metrics_endpoint_is_protected_when_token_configured(monkeypatch):
    from backend.main import app
    monkeypatch.setenv('HEALTHGUARD_METRICS_TOKEN','metrics-secret')
    c=TestClient(app)
    assert c.get('/api/ops/metrics').status_code==403
    r=c.get('/api/ops/metrics',headers={'X-Metrics-Token':'metrics-secret'})
    assert r.status_code==200
    assert 'healthguard_requests_total' in r.text
    assert 'metrics-secret' not in r.text


def test_readiness_does_not_leak_dependency_error_details(monkeypatch):
    from backend.main import readiness
    class BadEngine:
        def connect(self): raise RuntimeError('postgres://secret-user:secret-password@internal-db/healthguard')
    monkeypatch.setattr('db.session.engine',BadEngine())
    r=readiness()
    assert r.status_code in {200,503}
    assert 'secret-password' not in r.body.decode()
    assert 'internal-db' not in r.body.decode()


def test_audit_context_redacts_sensitive_fields():
    from domain.observability import sanitize_context
    result=sanitize_context({'patient_id':'patient-1','question':'[REDACTED]','api_key':'secret','safe':'ok'})
    assert result['patient_id']=='patient-1'
    assert result['safe']=='ok'
    assert 'api_key' not in result
    assert result['question']=='[REDACTED]'


def test_db_observability_hooks_exist():
    text=open('db/session.py').read()
    assert 'before_cursor_execute' in text
    assert 'after_cursor_execute' in text
    assert 'handle_error' in text


def test_storage_operations_emit_safe_metrics(monkeypatch, tmp_path):
    import storage.service as service
    from domain.observability import prometheus
    s=service.Storage(); s.backend='local'; s.root=tmp_path
    source=tmp_path/'source.txt'; source.write_bytes(b'abc')
    s.put_file('patients/p/doc/file.pdf',source,'application/pdf')
    assert s.get('patients/p/doc/file.pdf')==b'abc'
    s.delete('patients/p/doc/file.pdf')
    text=prometheus()
    assert 'healthguard_storage_operations_total' in text
    assert 'abc' not in text


def test_ai_transport_observability_does_not_log_request_content():
    text=open('ai/provider_utils.py').read()
    assert 'healthguard_ai_provider_calls_total' in text
    assert 'healthguard_ai_provider_latency' in text
    assert 'request.data' in text  # parsed only for safe model metadata
    assert 'logger' not in text


def test_worker_metrics_cover_lifecycle_signals():
    text=open('jobs/worker.py').read()
    for marker in ['healthguard_jobs_processing_total','healthguard_jobs_completed_total','healthguard_jobs_failed_total','healthguard_job_retries_total','healthguard_jobs_recovered_total']:
        assert marker in text


def test_queue_metrics_cover_poison_and_dependency_events():
    text=open('jobs/queue.py').read()
    for marker in ['healthguard_queue_poison_messages_total','healthguard_dependency_failures_total','healthguard_jobs_enqueue_total','healthguard_jobs_claim_total']:
        assert marker in text


def test_metrics_do_not_expose_raw_resource_ids():
    from domain.observability import inc, prometheus
    inc("healthguard_requests_total", route="/api/patients/11111111-2222-3333-4444-555555555555", status="200")
    text=prometheus()
    assert "11111111-2222-3333-4444-555555555555" not in text


def test_metrics_are_prometheus_client_metrics_not_custom_in_memory_state():
    import domain.observability as obs
    from prometheus_client import Counter
    obs.inc("healthguard_phase4_prometheus_test_total", safe="ok")
    assert isinstance(obs._counters["healthguard_phase4_prometheus_test_total"], Counter)
    text = obs.prometheus()
    assert "healthguard_phase4_prometheus_test_total" in text


def test_metric_labels_explicitly_reject_patient_user_document_identifiers():
    from domain.observability import inc, prometheus
    inc(
        "healthguard_requests_total",
        method="GET",
        route="/api/patients/11111111-2222-3333-4444-555555555555/documents/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        status="200",
        patient_id="patient-secret",
        user_id="user-secret",
        document_id="document-secret",
    )
    text = prometheus()
    assert "11111111-2222-3333-4444-555555555555" not in text
    assert "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa" not in text
    assert "patient-secret" not in text
    assert "user-secret" not in text
    assert "document-secret" not in text


def test_worker_heartbeat_uses_bounded_ttl_and_safe_identity(monkeypatch):
    import jobs.worker as worker
    calls = {}
    class FakeClient:
        def set(self, key, value, ex):
            calls.update(key=key, value=value, ex=ex)
    monkeypatch.setattr(worker, "_client", lambda: FakeClient())
    monkeypatch.setattr(worker, "WORKER_ID", "worker-test")
    assert worker.worker_heartbeat() is True
    assert worker.WORKER_METRIC_ID != worker.WORKER_ID
    assert calls["key"] == "healthguard:worker:heartbeat:worker-test"
    assert calls["ex"] >= 30
    assert "patient" not in calls["key"]


def test_worker_heartbeat_failure_does_not_raise(monkeypatch):
    import jobs.worker as worker
    from jobs.queue import QueueUnavailable
    monkeypatch.setattr(worker, "_client", lambda: (_ for _ in ()).throw(QueueUnavailable("unavailable")))
    assert worker.worker_heartbeat() is False


def test_structured_logger_allowlist_drops_unapproved_extra_fields():
    import logging
    from utils.logger import _JSONFormatter
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "safe", (), None)
    record.request_id = "req-1"
    record.patient_id = "patient-secret"
    record.api_key = "secret"
    record.route = "/api/health"
    payload = json.loads(_JSONFormatter().format(record))
    assert payload["request_id"] == "req-1"
    assert payload["route"] == "/api/health"
    assert "patient_id" not in payload
    assert "api_key" not in payload


def test_prometheus_multiprocess_mode_aggregates_across_process(tmp_path):
    import os, subprocess, sys
    code = """
from domain.observability import inc
inc('healthguard_multiprocess_test_total', outcome='success')
"""
    env = os.environ.copy()
    env['PROMETHEUS_MULTIPROC_DIR'] = str(tmp_path)
    env['PYTHONPATH'] = os.getcwd()
    p1 = subprocess.run([sys.executable, '-c', code], env=env, capture_output=True, text=True)
    p2 = subprocess.run([sys.executable, '-c', code], env=env, capture_output=True, text=True)
    assert p1.returncode == 0, p1.stderr
    assert p2.returncode == 0, p2.stderr
    scrape = """
from domain.observability import prometheus
print(prometheus())
"""
    p3 = subprocess.run([sys.executable, '-c', scrape], env=env, capture_output=True, text=True)
    assert p3.returncode == 0, p3.stderr
    assert 'healthguard_multiprocess_test_total' in p3.stdout
    assert ' 2.0' in p3.stdout or ' 2' in p3.stdout
