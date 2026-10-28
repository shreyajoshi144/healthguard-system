from __future__ import annotations

import os
import re
import time
from contextlib import contextmanager
from threading import Lock

from prometheus_client import Counter, Gauge, Histogram, REGISTRY, generate_latest
from prometheus_client import multiprocess

# Production deployments that run multiple worker processes should set
# PROMETHEUS_MULTIPROC_DIR to a writable, process-shared directory and clear it
# before starting a new process group. prometheus_client then persists metric
# state outside the Python process and aggregates it at scrape time.
PROMETHEUS_MULTIPROC_DIR = os.getenv("PROMETHEUS_MULTIPROC_DIR")
if PROMETHEUS_MULTIPROC_DIR:
    os.makedirs(PROMETHEUS_MULTIPROC_DIR, exist_ok=True)

SENSITIVE_KEYS = {
    "password", "token", "api_key", "secret", "secret_key", "authorization",
    "cookie", "prompt", "raw_prompt", "raw_response", "document_content",
    "source_text", "medical_text", "email", "phone",
}
METRIC_FORBIDDEN_LABELS = {
    "patient_id", "user_id", "document_id", "resource_id", "health_fact_id",
    "medication_id", "investigation_id", "handoff_id", "health_action_id", "share_id",
}
SENSITIVE_VALUE_PATTERNS = re.compile(
    r"(?i)(?:bearer\s+|sk-[A-Za-z0-9_-]+|[0-9a-f]{32,}|[0-9a-f]{8}-[0-9a-f-]{27,})"
)

# Fixed label schemas prevent accidental high-cardinality identifiers from
# becoming Prometheus labels. Every production metric has an explicit schema.
_SCHEMAS: dict[str, tuple[str, ...]] = {
    "healthguard_requests_total": ("method", "route", "status"),
    "healthguard_requests_errors_total": ("status", "error_class", "route"),
    "healthguard_requests_in_flight": (),
    "healthguard_request_latency": ("method", "route"),
    "healthguard_dependency_failures_total": ("dependency", "operation", "error_type"),
    "healthguard_db_operations_total": ("outcome", "error_type"),
    "healthguard_db_query_duration": ("outcome",),
    "healthguard_storage_operations_total": ("operation", "outcome", "error_type"),
    "healthguard_storage_operation_duration": ("operation",),
    "healthguard_storage_health_duration": ("dependency",),
    "healthguard_dependency_health_checks_total": ("dependency", "outcome"),
    "healthguard_jobs_enqueue_total": ("outcome",),
    "healthguard_jobs_claim_total": ("outcome",),
    "healthguard_jobs_ack_total": ("outcome",),
    "healthguard_jobs_processing_total": ("outcome",),
    "healthguard_jobs_completed_total": ("outcome",),
    "healthguard_jobs_failed_total": ("error_type",),
    "healthguard_job_retries_total": ("outcome",),
    "healthguard_jobs_queue_state_total": ("state", "outcome"),
    "healthguard_jobs_recovered_total": ("reason",),
    "healthguard_job_processing_duration": ("outcome",),
    "healthguard_worker_dependency_failures_total": ("dependency", "error_type"),
    "healthguard_queue_enqueue_duration": ("component",),
    "healthguard_queue_poison_messages_total": ("location",),
    "healthguard_ai_provider_calls_total": ("outcome", "model"),
    "healthguard_ai_provider_attempts_total": ("outcome", "model", "status", "error_type"),
    "healthguard_ai_provider_latency": ("model",),
    "healthguard_ai_tokens_total": ("model", "kind"),
    "healthguard_alert_delivery_total": ("channel", "outcome"),
    "healthguard_alerts_undelivered": ("scope",),
    "healthguard_alert_oldest_undelivered_age_seconds": ("scope",),
    "healthguard_ai_served_model_total": ("requested", "served"),
    "healthguard_ai_usage_missing_total": ("model",),
    "healthguard_ai_cost_micro_total": ("model",),
    "healthguard_ai_safety_rejections_total": ("surface",),
    "healthguard_ai_grounding_rejections_total": ("surface", "reason"),
    "healthguard_ai_insufficient_evidence_total": ("surface",),
    "healthguard_audit_events_total": ("action", "resource_type", "outcome"),
    "healthguard_worker_heartbeat": ("worker",),
    "healthguard_worker_heartbeat_age_seconds": ("worker",),
}

_lock = Lock()
_counters: dict[str, Counter] = {}
_gauges: dict[str, Gauge] = {}
_histograms: dict[str, Histogram] = {}


def _metric_name(name: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9_:]", "_", str(name))
    return name if name and re.match(r"^[a-zA-Z_:]", name) else f"healthguard_{name}"


def _safe_label_value(value: object, limit: int = 80) -> str:
    text = str(value if value is not None else "unknown").replace("\n", " ").replace("\r", " ")
    # UUIDs, long opaque IDs and token-like values must never become labels.
    if SENSITIVE_VALUE_PATTERNS.search(text):
        return "redacted"
    return text[:limit]


def _safe_labels(name: str, labels: dict) -> dict[str, str]:
    schema = _SCHEMAS.get(name)
    if schema is None:
        # Compatibility for tests/internal callers: unknown metrics may only
        # use their first safe label schema. Production metrics are all explicit.
        allowed = [str(k) for k in labels if str(k).lower() not in SENSITIVE_KEYS and str(k).lower() not in METRIC_FORBIDDEN_LABELS]
        schema = tuple(sorted(allowed))
        _SCHEMAS[name] = schema
    safe = {}
    for key in schema:
        value = labels.get(key, "unknown")
        if key.lower() in SENSITIVE_KEYS or key.lower() in METRIC_FORBIDDEN_LABELS:
            value = "redacted"
        value = _safe_label_value(value)
        if key in {"route", "path"}:
            value = re.sub(r"/[0-9]+(?=/|$)", "/:id", value)
            value = re.sub(r"/[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}(?=/|$)", "/:id", value)
        safe[key] = value
    return safe


def _counter(name: str) -> Counter:
    with _lock:
        if name not in _counters:
            labels = _SCHEMAS.get(name, ())
            _counters[name] = Counter(name, f"HealthGuard counter {name}", labelnames=labels)
        return _counters[name]


def _gauge(name: str) -> Gauge:
    with _lock:
        if name not in _gauges:
            labels = _SCHEMAS.get(name, ())
            _gauges[name] = Gauge(
                name, f"HealthGuard gauge {name}", labelnames=labels,
                multiprocess_mode="livesum" if PROMETHEUS_MULTIPROC_DIR else "all",
            )
        return _gauges[name]


def _histogram(name: str) -> Histogram:
    with _lock:
        if name not in _histograms:
            labels = _SCHEMAS.get(name, ())
            _histograms[name] = Histogram(name, f"HealthGuard histogram {name}", labelnames=labels)
        return _histograms[name]


def inc(name: str, value: int = 1, **labels: object) -> None:
    name = _metric_name(name)
    safe = _safe_labels(name, labels)
    _counter(name).labels(**safe).inc(value) if safe else _counter(name).inc(value)


def set_gauge(name: str, value: float, **labels: object) -> None:
    name = _metric_name(name)
    safe = _safe_labels(name, labels)
    _gauge(name).labels(**safe).set(value) if safe else _gauge(name).set(value)


def add_gauge(name: str, delta: float, **labels: object) -> None:
    name = _metric_name(name)
    safe = _safe_labels(name, labels)
    _gauge(name).labels(**safe).inc(delta) if safe else _gauge(name).inc(delta)


def observe(name: str, duration_ms: float, **labels: object) -> None:
    name = _metric_name(name)
    safe = _safe_labels(name, labels)
    _histogram(name).labels(**safe).observe(float(duration_ms)) if safe else _histogram(name).observe(float(duration_ms))


def snapshot() -> dict:
    """Compatibility view for tests; production scraping uses Prometheus collectors."""
    output = {"counters": [], "gauges": [], "durations": []}
    text = prometheus()
    for line in text.splitlines():
        if line.startswith("#") or "{" not in line:
            continue
        name, rest = line.split(" ", 1)
        labels_text, value = rest.rsplit(" ", 1)
        labels = {}
        if labels_text.startswith("{"):
            for item in re.findall(r'(\w+)="((?:\\.|[^"\\])*)"', labels_text):
                labels[item[0]] = item[1]
        row = {"name": name, "labels": labels, "value": float(value)}
        if name.endswith("_count"):
            output["durations"].append(row)
        elif name.endswith("_sum"):
            continue
        elif name.endswith("_total"):
            output["counters"].append(row)
        else:
            output["gauges"].append(row)
    return output


def prometheus() -> str:
    if PROMETHEUS_MULTIPROC_DIR:
        registry = REGISTRY.__class__()  # CollectorRegistry
        multiprocess.MultiProcessCollector(registry)
        return generate_latest(registry).decode("utf-8")
    return generate_latest(REGISTRY).decode("utf-8")


@contextmanager
def timed(name: str, **labels: object):
    started = time.monotonic()
    try:
        yield
        inc(name + "_total", outcome="success", **labels)
    except Exception as exc:
        inc(name + "_total", outcome="failure", error_type=type(exc).__name__, **labels)
        raise
    finally:
        observe(name + "_duration", (time.monotonic() - started) * 1000, **labels)


def sanitize_context(context: dict | None) -> dict | None:
    if context is None:
        return None
    safe = {}
    for key, value in context.items():
        normalized = str(key).lower()
        if normalized in SENSITIVE_KEYS or any(token in normalized for token in SENSITIVE_KEYS):
            continue
        if isinstance(value, (bool, int, float)) or value is None:
            safe[str(key)] = value
        elif isinstance(value, str):
            if normalized.endswith("_sha256") and re.fullmatch(r"[0-9a-fA-F]{64}", value):
                safe[str(key)] = value.lower()
            else:
                safe[str(key)] = _safe_label_value(value, 256)
        elif isinstance(value, (list, tuple)):
            safe[str(key)] = [_safe_label_value(x, 128) for x in value[:50]]
        elif isinstance(value, dict):
            safe[str(key)] = sanitize_context(value)
    return safe
