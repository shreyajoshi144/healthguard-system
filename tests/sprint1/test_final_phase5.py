import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from domain.email_verification import public_base_url
from jobs.queue import QueueUnavailable, enqueue_document

ROOT=Path(__file__).resolve().parents[2]


def test_frontend_auth_guard_has_no_browser_token_reference():
    source=(ROOT/"frontend/js/api.js").read_text()
    assert "hgSetSession(token, user)" not in source
    assert "hgSetSession(null, user)" in source
    assert "localStorage.setItem" not in source


def test_production_requires_public_base_url(monkeypatch):
    monkeypatch.setenv("APP_ENV","production")
    monkeypatch.delenv("HEALTHGUARD_PUBLIC_BASE_URL",raising=False)
    with pytest.raises(RuntimeError,match="PUBLIC_BASE_URL"):
        public_base_url()


def test_production_public_base_url_must_use_https(monkeypatch):
    monkeypatch.setenv("APP_ENV","production")
    monkeypatch.setenv("HEALTHGUARD_PUBLIC_BASE_URL","http://healthguard.example.com")
    with pytest.raises(RuntimeError,match="HTTPS"):
        public_base_url()


def test_production_rejects_localhost_public_base_url(monkeypatch):
    monkeypatch.setenv("APP_ENV","production")
    monkeypatch.setenv("HEALTHGUARD_PUBLIC_BASE_URL","https://localhost:8000")
    with pytest.raises(RuntimeError,match="development hostname"):
        public_base_url()


def test_production_rejects_loopback_public_base_url(monkeypatch):
    monkeypatch.setenv("APP_ENV","production")
    monkeypatch.setenv("HEALTHGUARD_PUBLIC_BASE_URL","https://127.0.0.1:8000")
    with pytest.raises(RuntimeError,match="development hostname"):
        public_base_url()


def test_valid_production_public_base_url(monkeypatch):
    monkeypatch.setenv("APP_ENV","production")
    monkeypatch.setenv("HEALTHGUARD_PUBLIC_BASE_URL","https://healthguard.example.com")
    assert public_base_url()=="https://healthguard.example.com"


def test_production_queue_failure_is_explicit(monkeypatch):
    monkeypatch.setenv("APP_ENV","production")
    monkeypatch.setenv("REDIS_URL","redis://127.0.0.1:63999/0")
    import jobs.queue as queue
    monkeypatch.setattr(queue,"redis",None)
    with pytest.raises(QueueUnavailable):
        enqueue_document("00000000-0000-0000-0000-000000000001")


def test_production_cors_configuration_is_not_wildcard():
    source=(ROOT/"backend/main.py").read_text()
    assert 'allow_origins=["*"]' not in source
    assert 'allow_credentials=True' in source
    assert "_validate_cors_origins" in source


def test_deletion_state_columns_are_migrated():
    source=(ROOT/"alembic/versions/0003_final_sprint1_hardening.py").read_text()
    assert '"users"' in source and '"deletion_pending"' in source
    assert '"patient_profiles"' in source


def test_phase5_final_migration_chain_is_linear():
    source=(ROOT/"alembic/versions/0003_final_sprint1_hardening.py").read_text()
    assert 'down_revision="0002_sprint1_hardening"' in source


def test_invalid_verification_tokens_are_controlled():
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from backend.main import app
    from db.models import Base
    from db.session import get_db
    engine=create_engine("sqlite://",connect_args={"check_same_thread":False},poolclass=StaticPool)
    @event.listens_for(engine,"connect")
    def _fk(conn, _record): conn.execute("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    Session=sessionmaker(bind=engine,expire_on_commit=False)
    db=Session()
    app.dependency_overrides[get_db]=lambda:db
    try:
        client=TestClient(app)
        for raw in ["random-token", "%%%malformed%%%", ""]:
            response=client.get("/api/auth/verify-email", params={"token":raw})
            assert response.status_code==400
            assert "invalid or expired" in response.json()["detail"].lower()
    finally:
        app.dependency_overrides.pop(get_db,None)
        db.close()
        engine.dispose()


def test_production_config_requires_core_services(monkeypatch):
    from backend.main import _validate_production_config
    monkeypatch.setenv("APP_ENV","production")
    monkeypatch.delenv("DATABASE_URL",raising=False)
    with pytest.raises(RuntimeError,match="Missing required production configuration"):
        _validate_production_config()


def test_production_cors_validation(monkeypatch):
    from backend.main import _validate_cors_origins
    monkeypatch.setenv("APP_ENV","production")
    with pytest.raises(RuntimeError,match="Wildcard"):
        _validate_cors_origins(["*"])
    with pytest.raises(RuntimeError,match="HTTPS"):
        _validate_cors_origins(["http://healthguard.example.com"])
    _validate_cors_origins(["https://healthguard.example.com"])


def test_login_response_does_not_expose_access_token():
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from backend.main import app
    from db.models import Base, User
    from db.session import get_db
    from backend.auth import _hash_password
    engine=create_engine("sqlite://",connect_args={"check_same_thread":False},poolclass=StaticPool)
    @event.listens_for(engine,"connect")
    def _fk(conn, _record): conn.execute("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    Session=sessionmaker(bind=engine,expire_on_commit=False)
    db=Session()
    u=User(username="phase8login",email="phase8login@example.com",password_hash=_hash_password("correct horse battery staple"),email_verified=True)
    db.add(u); db.commit()
    app.dependency_overrides[get_db]=lambda:db
    try:
        r=TestClient(app).post("/api/auth/login",json={"username":"phase8login@example.com","password":"correct horse battery staple"})
        assert r.status_code==200
        assert "access_token" not in r.json()
    finally:
        app.dependency_overrides.pop(get_db,None); db.close(); engine.dispose()


def test_frontend_has_no_web_storage_auth_state():
    from pathlib import Path
    source=Path("frontend/js/api.js").read_text()
    assert "localStorage" not in source
    assert "sessionStorage" not in source
    assert "access_token" not in source

def test_healthguard_frontend_covers_required_sprint1_workflows():
    health=(ROOT/"frontend/health.html").read_text()
    patient=(ROOT/"frontend/patient.html").read_text()
    index=(ROOT/"frontend/index.html").read_text()
    js=(ROOT/"frontend/js/patient.js").read_text()
    assert "Personal Health Continuity" in index
    for text in ["Health history","Medications","Investigations","Documents","Family access"]: assert text in patient
    for precision in ["EXACT","APPROXIMATE","YEAR","UNKNOWN"]: assert precision in js
    for event in ["CONSULTATION","HOSPITALIZATION","SURGERY","VACCINATION","ALLERGY_RECORDED","FOLLOW_UP"]: assert event in js
    for marker in ["retry-processing","data-med-event","data-med-history","data-result","data-results","data-revoke"]: assert marker in js
    assert "/api/patients/" in js
    assert "patient-satisfaction" not in index.lower()
    assert "sentiment" not in index.lower()


def test_security_headers_are_configured():
    source=(ROOT/"backend/main.py").read_text()
    for header in ["X-Content-Type-Options","X-Frame-Options","Referrer-Policy","Permissions-Policy","Content-Security-Policy","Strict-Transport-Security"]:
        assert header in source


def test_legacy_surface_is_not_primary_healthguard_navigation():
    for filename in ["index.html","workspace.html","health.html","patient.html","login.html","register.html"]:
        source=(ROOT/"frontend"/filename).read_text().lower()
        assert "/analyze" not in source
        assert "/insights" not in source
        assert "/departments" not in source
        assert "/models" not in source
