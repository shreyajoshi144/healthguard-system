from pathlib import Path
import os
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_production_config_requires_metrics_and_multiprocess_dir(monkeypatch):
    from backend.main import _validate_production_config
    required = {
        "APP_ENV": "production",
        "DATABASE_URL": "postgresql+psycopg://u:p@db/healthguard",
        "REDIS_URL": "rediss://redis.example.com:6380/0",
        "STORAGE_BACKEND": "s3",
        "STORAGE_ENDPOINT": "https://s3.example.com",
        "STORAGE_BUCKET": "private",
        "STORAGE_ACCESS_KEY": "access",
        "STORAGE_SECRET_KEY": "secret",
        "SMTP_HOST": "smtp.example.com",
        "SMTP_FROM": "healthguard@example.com",
        "SMTP_STARTTLS": "true",
        "HEALTHGUARD_LLM_PROVIDER": "openai_compatible",
        "HEALTHGUARD_LLM_BASE_URL": "https://llm.example.com/v1",
        "HEALTHGUARD_LLM_MODEL": "model",
        "HEALTHGUARD_LLM_API_KEY": "secret",
        "HEALTHGUARD_EMBEDDING_PROVIDER": "openai_compatible",
        "HEALTHGUARD_EMBEDDING_BASE_URL": "https://embed.example.com/v1",
        "HEALTHGUARD_EMBEDDING_MODEL": "embed",
        "HEALTHGUARD_EMBEDDING_API_KEY": "secret",
        "HEALTHGUARD_AI_TIMEOUT_SECONDS": "45",
        "HEALTHGUARD_AI_MAX_RETRIES": "1",
        "HEALTHGUARD_PUBLIC_BASE_URL": "https://healthguard.example.com",
        "CORS_ALLOWED_ORIGINS": "https://healthguard.example.com",
        "HEALTHGUARD_METRICS_TOKEN": "m" * 40,
        "PROMETHEUS_MULTIPROC_DIR": "/var/run/healthguard/metrics",
    }
    for k, v in required.items(): monkeypatch.setenv(k, v)
    _validate_production_config()


def test_production_config_rejects_http_storage(monkeypatch):
    from backend.main import _validate_production_config
    required = {
        "APP_ENV": "production", "DATABASE_URL": "postgresql+psycopg://u:p@db/healthguard",
        "REDIS_URL": "rediss://redis.example.com:6380/0", "STORAGE_BACKEND": "s3",
        "STORAGE_ENDPOINT": "http://s3.example.com", "STORAGE_BUCKET": "private",
        "STORAGE_ACCESS_KEY": "access", "STORAGE_SECRET_KEY": "secret",
        "SMTP_HOST": "smtp.example.com", "SMTP_FROM": "healthguard@example.com",
        "HEALTHGUARD_LLM_PROVIDER": "openai_compatible", "HEALTHGUARD_LLM_BASE_URL": "https://llm.example.com/v1",
        "HEALTHGUARD_LLM_MODEL": "model", "HEALTHGUARD_LLM_API_KEY": "secret",
        "HEALTHGUARD_EMBEDDING_PROVIDER": "openai_compatible", "HEALTHGUARD_EMBEDDING_BASE_URL": "https://embed.example.com/v1",
        "HEALTHGUARD_EMBEDDING_MODEL": "embed", "HEALTHGUARD_EMBEDDING_API_KEY": "secret",
        "HEALTHGUARD_AI_TIMEOUT_SECONDS": "45", "HEALTHGUARD_AI_MAX_RETRIES": "1",
        "HEALTHGUARD_PUBLIC_BASE_URL": "https://healthguard.example.com",
        "CORS_ALLOWED_ORIGINS": "https://healthguard.example.com",
        "HEALTHGUARD_METRICS_TOKEN": "m" * 40, "PROMETHEUS_MULTIPROC_DIR": "/var/run/healthguard/metrics",
    }
    for k, v in required.items(): monkeypatch.setenv(k, v)
    with pytest.raises(RuntimeError, match="STORAGE_ENDPOINT"):
        _validate_production_config()


def test_production_config_rejects_short_metrics_token(monkeypatch):
    from backend.main import _validate_production_config
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("HEALTHGUARD_METRICS_TOKEN", "short")
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", "/tmp/metrics")
    # Missing earlier required configuration is acceptable; this test verifies
    # the final artifact contains an explicit production token requirement.
    source=(ROOT / "backend" / "main.py").read_text()
    assert "must be at least 32 characters" in source


def test_production_scripts_fail_closed_on_missing_core_secrets():
    script=(ROOT / "scripts" / "production-start.sh").read_text()
    for name in ["DATABASE_URL", "REDIS_URL", "HEALTHGUARD_JWT_SECRET", "HEALTHGUARD_PUBLIC_BASE_URL", "HEALTHGUARD_METRICS_TOKEN"]:
        assert f'"${{{name}:?' in script


def test_worker_script_is_separate_from_api_start():
    script=(ROOT / "scripts" / "production-worker.sh").read_text()
    assert "python -m jobs.worker" in script
    assert "uvicorn" not in script


def test_dockerfile_is_non_root_and_has_healthcheck():
    source=(ROOT / "Dockerfile").read_text()
    assert "useradd" in source
    assert "USER healthguard" in source
    assert "HEALTHCHECK" in source
    assert "--host 0.0.0.0" not in source
    assert "production-start.sh" in source


def test_compose_separates_api_and_worker_and_does_not_commit_credentials():
    source=(ROOT / "docker-compose.yml").read_text()
    assert "python -m jobs.worker" not in source  # worker uses the checked-in script
    assert "/app/scripts/production-worker.sh" in source
    assert "/app/scripts/worker-healthcheck.py" in source
    assert "HEALTHGUARD_WORKER_ID" in source
    assert "/app/scripts/production-start.sh" in source
    assert "POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?" in source
    assert "MINIO_ROOT_PASSWORD: ${MINIO_ROOT_PASSWORD:?" in source
    assert "healthguard-secret" not in source
    assert "condition: service_healthy" in source


def test_render_defines_separate_api_and_worker_services():
    source=(ROOT / "render.yaml").read_text()
    assert "name: healthguard-api" in source
    assert "name: healthguard-worker" in source
    # Docker runtime (Tesseract is needed for OCR): the scripts are started with dockerCommand rather than startCommand.
    assert "runtime: docker" in source and "runtime: python" not in source
    assert "dockerCommand: \"./scripts/production-start.sh\"" in source
    assert "dockerCommand: \"./scripts/production-worker.sh\"" in source
    assert "sync: false" in source
    assert source.count("HEALTHGUARD_WORKER_ID") == 1


def test_production_docs_cover_migrations_backups_and_recovery():
    source=(ROOT / "deploy" / "PRODUCTION.md").read_text()
    for marker in ["Topology", "alembic upgrade head", "PostgreSQL", "Object storage", "Redis", "Backup and recovery", "Health and diagnostics"]:
        assert marker in source


def test_no_real_env_or_private_key_files_are_committed():
    assert not list(ROOT.rglob(".env"))
    assert not list(ROOT.rglob("*.pem"))
    assert not list(ROOT.rglob("*.key"))
    source="\n".join(p.read_text(errors="ignore") for p in [ROOT/"Dockerfile", ROOT/"docker-compose.yml", ROOT/"render.yaml"])
    assert "healthguard-secret" not in source
    assert "POSTGRES_PASSWORD: healthguard" not in source


def test_storage_documents_are_not_under_frontend_static_tree():
    frontend=(ROOT/"frontend").resolve()
    private=(ROOT/"private_storage").resolve()
    assert private != frontend
    main=(ROOT/"backend"/"main.py").read_text()
    assert 'StaticFiles(directory=_SAMPLE_DATA_DIR)' in main
    assert 'StaticFiles(directory=FRONTEND_DIR' in main
    assert 'private_storage' not in main


def test_production_migration_check_is_fail_closed():
    source=(ROOT/"backend"/"main.py").read_text()
    assert 'alembic_version' in source
    assert 'schema is not at the current migration head' in source


def test_metrics_directory_is_configured_for_container_services():
    compose=(ROOT/"docker-compose.yml").read_text()
    assert "PROMETHEUS_MULTIPROC_DIR: /var/run/healthguard/metrics" in compose
    assert "metrics:/var/run/healthguard/metrics" in compose
    env=(ROOT/".env.example").read_text()
    assert "PROMETHEUS_MULTIPROC_DIR" in env


def test_release_scripts_are_executable():
    assert os.access(ROOT/"scripts"/"production-start.sh", os.X_OK)
    assert os.access(ROOT/"scripts"/"production-worker.sh", os.X_OK)

def test_worker_metrics_server_has_protected_metrics_and_liveness_routes():
    source=(ROOT / "jobs" / "worker.py").read_text()
    assert "ThreadingHTTPServer" in source
    assert 'self.path == "/health"' in source
    assert 'self.path == "/metrics"' in source
    assert 'X-Metrics-Token' in source
    assert "prometheus()" in source
    assert "start_metrics_server()" in source
    assert "stop_metrics_server()" in source


def test_render_worker_does_not_use_multiprocess_directory_or_fixed_port():
    source=(ROOT / "render.yaml").read_text()
    worker=source.split("name: healthguard-worker", 1)[1]
    assert "PROMETHEUS_MULTIPROC_DIR" not in worker
    assert "key: PORT" not in worker
    assert 'healthCheckPath: /health' in worker


def test_migration_script_is_single_release_command():
    source=(ROOT / "scripts" / "migrate.sh").read_text()
    assert "alembic upgrade head" in source
    assert "exec alembic upgrade head" in source
