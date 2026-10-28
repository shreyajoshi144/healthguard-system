from __future__ import annotations
import json, logging, os, re, sys
from logging.handlers import RotatingFileHandler

LOG_DIR = "logs"
LOG_FILE = os.path.join(LOG_DIR, "app.log")
DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
ALLOWED_FIELDS = {
    "request_id", "correlation_id", "route", "method", "status", "latency_ms",
    "service", "component", "error_type", "error_class", "job_id", "event",
    "outcome", "retry_count", "dependency", "operation", "worker_id", "attempt",
}
SENSITIVE_FIELD_NAMES = {
    "password", "token", "api_key", "secret", "secret_key", "authorization",
    "cookie", "session", "share_token", "prompt", "raw_prompt", "raw_response",
    "document_content", "source_text", "medical_text", "patient_id", "user_id",
    "document_id", "resource_id", "email", "phone", "content", "body", "data",
}
SENSITIVE_PATTERNS = re.compile(
    r"(?i)(?:bearer\s+[A-Za-z0-9._-]+|sk-[A-Za-z0-9_-]+|password\s*[=:]|api[_-]?key\s*[=:]|authorization\s*[=:])"
)
os.makedirs(LOG_DIR, exist_ok=True)


class _RedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Redact the rendered message if it contains an obvious secret-bearing pattern.
        message = str(record.getMessage())
        if SENSITIVE_PATTERNS.search(message):
            record.msg = "[REDACTED SENSITIVE LOG MESSAGE]"
            record.args = ()
        return True


class _JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, DATE_FORMAT),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ALLOWED_FIELDS:
            if hasattr(record, key):
                value = getattr(record, key)
                if key in SENSITIVE_FIELD_NAMES:
                    continue
                if isinstance(value, (str, int, float, bool)) or value is None:
                    payload[key] = value
                else:
                    payload[key] = str(value)[:256]
        if record.exc_info:
            payload["exception"] = type(record.exc_info[1]).__name__
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _configure_root_logger() -> None:
    root = logging.getLogger()
    if root.handlers:
        return
    production = os.getenv("APP_ENV", "development").lower() == "production"
    configured_level = os.getenv("HEALTHGUARD_LOG_LEVEL", "INFO" if production else "DEBUG").upper()
    root.setLevel(getattr(logging, configured_level, logging.INFO))
    redaction_filter = _RedactionFilter()
    formatter = _JSONFormatter()
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(getattr(logging, configured_level, logging.INFO))
    console.addFilter(redaction_filter)
    console.setFormatter(formatter)
    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    file_handler.setLevel(getattr(logging, configured_level, logging.INFO))
    file_handler.addFilter(redaction_filter)
    file_handler.setFormatter(formatter)
    root.addHandler(console)
    root.addHandler(file_handler)


_configure_root_logger()


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
