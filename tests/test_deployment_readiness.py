"""Deployment-readiness regressions (Docker Compose dev config, Render, OCR runtime, SMTP/Resend, production secrets).

Each test pins a defect found when auditing the supplied release package. Nothing here talks to a real provider or cloud:
SMTP is a protocol-level capture server on localhost, and OCR uses the locally installed Tesseract.
"""
from __future__ import annotations

import base64
import re
import socketserver
import subprocess
import threading
from pathlib import Path
from urllib.parse import urlparse

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _env_file(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip()
    return out


@pytest.fixture
def dev_env() -> dict[str, str]:
    return _env_file(ROOT / ".env.example")


# ----------------------------------------------------------------------------------------------------------------------
# Docker Compose with the documented `cp .env.example .env`
# ----------------------------------------------------------------------------------------------------------------------
def test_env_example_database_credentials_match_the_postgres_container(dev_env):
    """The postgres container is initialised from POSTGRES_*; DATABASE_URL pointing at another password makes migrate/api/worker fail to connect."""
    url = urlparse(dev_env["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://"))
    assert (url.username, url.password) == (dev_env["POSTGRES_USER"], dev_env["POSTGRES_PASSWORD"])
    assert url.path.lstrip("/") == dev_env["POSTGRES_DB"]
    assert url.hostname == "postgres"


def test_env_example_storage_credentials_match_the_minio_container(dev_env):
    assert dev_env["STORAGE_ACCESS_KEY"] == dev_env["MINIO_ROOT_USER"]
    assert dev_env["STORAGE_SECRET_KEY"] == dev_env["MINIO_ROOT_PASSWORD"]
    assert urlparse(dev_env["STORAGE_ENDPOINT"]).hostname == "minio"


@pytest.mark.parametrize("script", ["production-start.sh", "production-worker.sh"])
def test_documented_dev_env_satisfies_the_container_start_script_guards(dev_env, script):
    """`: "${VAR:?}"` aborts on an EMPTY value, so the shipped .env.example must define every guarded variable (the metrics token was empty)."""
    guards = [line for line in (ROOT / "scripts" / script).read_text().splitlines() if line.startswith(': "${')]
    assert guards
    result = subprocess.run(["sh", "-c", "set -eu\n" + "\n".join(guards)], env={"PATH": "/usr/bin:/bin", **dev_env}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_env_example_dev_tokens_meet_minimum_lengths(dev_env):
    assert len(dev_env["HEALTHGUARD_METRICS_TOKEN"]) >= 32
    assert len(dev_env["HEALTHGUARD_JWT_SECRET"]) >= 32


def test_compose_ships_a_free_dev_mailbox_wired_to_the_documented_smtp_settings(dev_env):
    compose = (ROOT / "docker-compose.yml").read_text()
    assert re.search(r"^  mailpit:\n    image: axllent/mailpit", compose, re.M)
    assert dev_env["SMTP_HOST"] == "mailpit" and dev_env["SMTP_PORT"] == "1025"
    assert dev_env["SMTP_STARTTLS"].lower() == "false" and dev_env["SMTP_FROM"] and dev_env["SMTP_PASSWORD"] == ""
    for service in ("api", "worker"):
        block = re.search(rf"^  {service}:\n(.*?)(?=^  \S)", compose, re.M | re.S).group(1)
        assert "mailpit:\n        condition: service_started" in block, f"{service} must wait for the dev mailbox"
    assert '"127.0.0.1:8025:8025"' in compose and '"127.0.0.1:1025:1025"' in compose, "dev mailbox must not be exposed beyond localhost"


def test_dev_mailbox_defaults_are_rejected_in_production(dev_env):
    from utils.production_config import validate_smtp
    with pytest.raises(RuntimeError, match="SMTP_HOST cannot be a development mailbox"):
        validate_smtp({**dev_env, "APP_ENV": "production"})


# ----------------------------------------------------------------------------------------------------------------------
# Render
# ----------------------------------------------------------------------------------------------------------------------
def _render_services() -> dict[str, str]:
    text = (ROOT / "render.yaml").read_text()
    head, api, worker = re.split(r"^  - type: web\n    name: healthguard-(?:api|worker)\n", text, flags=re.M)
    return {"api": api, "worker": worker}


def test_render_builds_both_services_from_the_dockerfile_so_tesseract_exists():
    docker = (ROOT / "Dockerfile").read_text()
    assert re.search(r"apt-get install[^\n]*tesseract-ocr", docker.replace("\\\n", " "))
    for name, block in _render_services().items():
        assert "runtime: docker" in block and "runtime: python" not in block, f"{name} must use the Docker runtime (native Python has no Tesseract)"
        assert "buildCommand" not in block and "startCommand" not in block


def test_render_docker_commands_and_health_checks():
    svc = _render_services()
    assert 'dockerCommand: "./scripts/production-start.sh"' in svc["api"] and "healthCheckPath: /api/health" in svc["api"]
    assert 'dockerCommand: "./scripts/production-worker.sh"' in svc["worker"] and "healthCheckPath: /health" in svc["worker"]
    assert 'preDeployCommand: "./scripts/migrate.sh"' in svc["api"]
    assert re.search(r'key: PORT\n\s+value: "8000"', svc["api"]) and "key: PORT" not in svc["worker"]


def test_render_worker_has_every_email_setting_the_api_has():
    """The worker sends reminders and alert email and builds their links; without SMTP_*/public URL every reminder failed permanently."""
    svc = _render_services()
    keys = lambda block: set(re.findall(r"- key: (\w+)", block))
    smtp_keys = {k for k in keys(svc["api"]) if k.startswith("SMTP_")} | {"HEALTHGUARD_PUBLIC_BASE_URL"}
    assert {"SMTP_HOST", "SMTP_FROM", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_STARTTLS", "HEALTHGUARD_PUBLIC_BASE_URL"} <= smtp_keys
    assert smtp_keys <= keys(svc["worker"])
    assert keys(svc["api"]) - keys(svc["worker"]) == {"CORS_ALLOWED_ORIGINS", "PORT"}   # the only intended API-only settings


# ----------------------------------------------------------------------------------------------------------------------
# Production secret / config validation
# ----------------------------------------------------------------------------------------------------------------------
def _prod(**overrides) -> dict[str, str]:
    env = {"APP_ENV": "production", "SMTP_HOST": "smtp.resend.com", "SMTP_PORT": "587", "SMTP_USERNAME": "resend", "SMTP_PASSWORD": "re_live_key",
           "SMTP_FROM": "HealthGuard <no-reply@mail.example.com>", "SMTP_STARTTLS": "true", "HEALTHGUARD_METRICS_TOKEN": "m" * 40,
           "HEALTHGUARD_JWT_SECRET": "j" * 48, "REDIS_URL": "rediss://redis.example.com:6380/0", "HEALTHGUARD_PUBLIC_BASE_URL": "https://healthguard.example.com"}
    env.update(overrides)
    return {k: v for k, v in env.items() if v is not None}


@pytest.mark.parametrize("name", ["HEALTHGUARD_JWT_SECRET", "HEALTHGUARD_METRICS_TOKEN", "HEALTHGUARD_ADMIN_TOTP_KEY", "STORAGE_SECRET_KEY", "SMTP_PASSWORD"])
def test_production_rejects_placeholder_secrets_from_env_example(name, dev_env):
    from utils.production_config import validate_secrets
    placeholder = "replace-with-a-long-random-secret-value-0000"
    with pytest.raises(RuntimeError, match=name) as exc:
        validate_secrets(_prod(**{name: placeholder}))
    assert placeholder not in str(exc.value), "the secret value must never appear in the error"
    validate_secrets({**dev_env, "APP_ENV": "development"})             # development keeps the shipped placeholders
    with pytest.raises(RuntimeError, match="HEALTHGUARD_JWT_SECRET"):  # copying .env.example to production must fail closed
        validate_secrets({**dev_env, "APP_ENV": "production"})


def test_production_rejects_short_admin_totp_key_that_would_silently_disable_admin():
    from utils.production_config import validate_secrets
    with pytest.raises(RuntimeError, match="HEALTHGUARD_ADMIN_TOTP_KEY must be at least 32"):
        validate_secrets(_prod(HEALTHGUARD_ADMIN_TOTP_KEY="short"))
    validate_secrets(_prod(HEALTHGUARD_ADMIN_TOTP_KEY="t" * 40))
    validate_secrets(_prod())                                           # unset stays valid: /admin is simply disabled


@pytest.mark.parametrize("override,message", [
    ({"SMTP_PORT": "465"}, "SMTP_PORT=465"),
    ({"SMTP_PORT": "abc"}, "valid port"),
    ({"SMTP_STARTTLS": "false"}, "STARTTLS"),
    ({"SMTP_HOST": "localhost"}, "development mailbox"),
    ({"SMTP_HOST": "mailpit"}, "development mailbox"),
    ({"SMTP_PASSWORD": None}, "set together"),
    ({"SMTP_USERNAME": None}, "set together"),
    ({"SMTP_FROM": "not-an-address"}, "email address"),
    ({"SMTP_FROM": None}, "must be configured"),
])
def test_production_smtp_validation_rejects_misconfiguration(override, message):
    from utils.production_config import validate_smtp
    with pytest.raises(RuntimeError, match=message):
        validate_smtp(_prod(**override))


def test_production_smtp_validation_accepts_resend_over_starttls_and_is_a_noop_in_development():
    from utils.production_config import validate_smtp
    validate_smtp(_prod())
    validate_smtp(_prod(SMTP_USERNAME=None, SMTP_PASSWORD=None))        # an unauthenticated relay is still allowed
    validate_smtp({"APP_ENV": "development", "SMTP_HOST": "", "SMTP_STARTTLS": "false"})


def test_worker_config_requires_what_the_reminder_dispatcher_needs():
    from utils.production_config import validate_worker_config
    pbu = lambda: "https://healthguard.example.com"
    validate_worker_config(_prod(), public_base_url=pbu)
    with pytest.raises(RuntimeError, match="SMTP_HOST and SMTP_FROM"):
        validate_worker_config(_prod(SMTP_HOST=None), public_base_url=pbu)

    def broken():
        raise RuntimeError("HEALTHGUARD_PUBLIC_BASE_URL must be configured in production.")
    with pytest.raises(RuntimeError, match="PUBLIC_BASE_URL"):
        validate_worker_config(_prod(), public_base_url=broken)
    with pytest.raises(RuntimeError, match="METRICS_TOKEN"):
        validate_worker_config(_prod(HEALTHGUARD_METRICS_TOKEN="short"), public_base_url=pbu)
    with pytest.raises(RuntimeError, match="REDIS_URL"):
        validate_worker_config(_prod(REDIS_URL="http://x"), public_base_url=pbu)
    validate_worker_config(_prod(SMTP_HOST=None, HEALTHGUARD_REMINDERS_ENABLED="false"))   # reminders off => worker sends no mail
    validate_worker_config({"APP_ENV": "development"})


# ----------------------------------------------------------------------------------------------------------------------
# One SMTP sender for verification, reminders and alerts (works with any STARTTLS relay, e.g. Resend; dev mailbox for local)
# ----------------------------------------------------------------------------------------------------------------------
class _SMTPHandler(socketserver.StreamRequestHandler):
    def handle(self):
        srv, w, data = self.server, self.wfile.write, None
        w(b"220 capture ESMTP\r\n")
        sender, rcpts = None, []
        while True:
            line = self.rfile.readline()
            if not line:
                return
            cmd = line.decode(errors="replace").rstrip("\r\n"); up = cmd.upper()
            if data is not None:
                if cmd == ".":
                    srv.messages.append((sender, list(rcpts), "\n".join(data))); w(b"250 queued\r\n"); data = None
                else:
                    data.append(cmd[1:] if cmd.startswith("..") else cmd)
                continue
            if up.startswith("EHLO"): w(b"250-capture\r\n250 AUTH PLAIN\r\n")
            elif up.startswith("AUTH PLAIN"):
                _, user, password = base64.b64decode(cmd.split(" ", 2)[2]).decode().split("\x00"); srv.logins.append((user, password)); w(b"235 ok\r\n")
            elif up.startswith("MAIL FROM:"): sender = cmd[10:].strip().strip("<>"); rcpts = []; w(b"250 OK\r\n")
            elif up.startswith("RCPT TO:"): rcpts.append(cmd[8:].strip().strip("<>")); w(b"250 OK\r\n")
            elif up == "DATA": w(b"354 go\r\n"); data = []
            elif up == "QUIT": w(b"221 bye\r\n"); return
            else: w(b"250 OK\r\n")


class _CaptureSMTP(socketserver.ThreadingTCPServer):
    allow_reuse_address = True; daemon_threads = True
    def __init__(self):
        super().__init__(("127.0.0.1", 0), _SMTPHandler); self.messages: list = []; self.logins: list = []


@pytest.fixture
def smtp(monkeypatch):
    server = _CaptureSMTP(); threading.Thread(target=server.serve_forever, daemon=True).start()
    for key, value in {"SMTP_HOST": "127.0.0.1", "SMTP_PORT": str(server.server_address[1]), "SMTP_FROM": "healthguard@healthguard.localhost", "SMTP_STARTTLS": "false",
                       "HEALTHGUARD_PUBLIC_BASE_URL": "http://localhost:8000", "APP_ENV": "development"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("SMTP_USERNAME", raising=False); monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    yield server
    server.shutdown(); server.server_close()


def test_mailer_sends_to_one_or_many_recipients_with_login_when_configured(smtp, monkeypatch):
    from domain.mailer import send_email
    send_email(to="a@example.test", subject="Hello", body="body one")
    send_email(to=["x@example.test", "y@example.test"], subject="Alert", body="body two")
    assert [m[1] for m in smtp.messages] == [["a@example.test"], ["x@example.test", "y@example.test"]]
    assert smtp.logins == []
    monkeypatch.setenv("SMTP_USERNAME", "resend"); monkeypatch.setenv("SMTP_PASSWORD", "re_key")
    send_email(to="a@example.test", subject="Hello", body="body three")
    assert smtp.logins == [("resend", "re_key")]


def test_mailer_classifies_failures_without_leaking_details(monkeypatch):
    from domain.mailer import EmailDeliveryError, send_email
    monkeypatch.delenv("SMTP_HOST", raising=False)
    with pytest.raises(EmailDeliveryError) as exc:
        send_email(to="a@example.test", subject="s", body="b")
    assert (exc.value.code, exc.value.permanent) == ("smtp_not_configured", True)
    monkeypatch.setenv("SMTP_HOST", "127.0.0.1"); monkeypatch.setenv("SMTP_FROM", "a@example.test"); monkeypatch.setenv("SMTP_PORT", "1"); monkeypatch.setenv("SMTP_STARTTLS", "false")
    with pytest.raises(EmailDeliveryError) as exc:
        send_email(to="a@example.test", subject="s", body="b")
    assert (exc.value.code, exc.value.permanent) == ("smtp_unavailable", False)
    with pytest.raises(EmailDeliveryError) as exc:
        send_email(to=[], subject="s", body="b")
    assert exc.value.code == "no_recipients"


# ----------------------------------------------------------------------------------------------------------------------
# Tesseract / OCR runtime (real binary: CI installs tesseract-ocr; a runtime without it must FAIL here, never skip)
# ----------------------------------------------------------------------------------------------------------------------
def test_check_ocr_runtime_reports_version_and_english_data():
    from jobs.extractors import check_ocr_runtime
    assert re.match(r"\d+\.\d+", check_ocr_runtime())


def test_check_ocr_runtime_fails_clearly_when_the_binary_is_missing(monkeypatch):
    import pytesseract
    from jobs.extractors import ExtractionError, check_ocr_runtime
    monkeypatch.setattr(pytesseract.pytesseract, "tesseract_cmd", "/nonexistent/tesseract")
    with pytest.raises(ExtractionError, match="not installed or cannot be executed"):
        check_ocr_runtime()


def test_check_ocr_runtime_requires_english_language_data(monkeypatch):
    import pytesseract
    from jobs.extractors import ExtractionError, check_ocr_runtime
    monkeypatch.setattr(pytesseract, "get_languages", lambda config="": ["osd", "fra"])
    with pytest.raises(ExtractionError, match="English language data"):
        check_ocr_runtime()


def test_extractor_really_ocrs_an_image_with_the_installed_tesseract():
    from io import BytesIO
    from PIL import Image, ImageDraw, ImageFont
    from jobs.extractors import extractor
    image = Image.new("RGB", (900, 160), "white")
    ImageDraw.Draw(image).text((30, 40), "HEMOGLOBIN 14.2 g/dL", fill="black", font=ImageFont.load_default(size=56))
    buffer = BytesIO(); image.save(buffer, format="PNG"); buffer.seek(0)
    (block,) = extractor.extract(buffer, "image/png")
    assert block.extraction_method == "OCR" and "HEMOGLOBIN" in block.source_text.upper() and block.confidence and block.confidence > 0.5


def test_ci_installs_tesseract_for_the_real_ocr_tests():
    assert "tesseract-ocr" in (ROOT / ".github" / "workflows" / "ci.yml").read_text()


# ----------------------------------------------------------------------------------------------------------------------
# Release packaging
# ----------------------------------------------------------------------------------------------------------------------
def test_release_zip_contains_deploy_files_keeps_script_modes_and_excludes_secrets_and_generated_files():
    import sys, tempfile, zipfile
    sys.path.insert(0, str(ROOT))
    from scripts.make_release import build_release
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "release.zip"
        build_release(ROOT, out, root_name="hg_p5_final")
        with zipfile.ZipFile(out) as z:
            names = {i.filename.split("/", 1)[1]: i for i in z.infolist()}
    for required in (".env.example", "Dockerfile", ".dockerignore", "docker-compose.yml", "render.yaml", "deploy/PRODUCTION.md", "utils/production_config.py",
                     "scripts/production-start.sh", "scripts/production-worker.sh", "scripts/migrate.sh", "alembic.ini"):
        assert required in names, required
    for script in ("scripts/production-start.sh", "scripts/production-worker.sh", "scripts/migrate.sh"):
        assert (names[script].external_attr >> 16) & 0o111, f"{script} lost its executable bit"
    assert not [n for n in names if n == ".env" or n.startswith(".env.") and n != ".env.example" or "__pycache__" in n or n.endswith((".pyc", ".pem", ".key", ".db", ".sqlite3", ".log"))
                or n.startswith(("logs/", "private_storage/"))]
