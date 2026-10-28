
from __future__ import annotations
import os
import hmac
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse
from typing import Optional

from fastapi import FastAPI, UploadFile, File, HTTPException, Depends, Request, Response, BackgroundTasks, Cookie, Header
from fastapi.middleware.cors import CORSMiddleware
from backend.body_limit import BodySizeLimitMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.exceptions import RequestValidationError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from pydantic import BaseModel, Field, EmailStr

from backend import auth
from db.models import EmailVerificationToken
from domain.email_verification import send_verification_email, public_base_url
from db.session import get_db
from db.models import User
from domain.audit import audit
from domain.rate_limit import rate_limit
from api.healthguard import router as healthguard_router
from api.measurements import router as measurements_router
from api.consumer import router as consumer_router
from api.admin import router as admin_router
from api.reminders import router as reminders_router
from api.privacy import router as privacy_router
from utils.logger import get_logger
from domain.observability import inc, observe, prometheus, add_gauge

logger = get_logger(__name__)

def _observed_route(request: Request) -> str:
    route = getattr(request.scope.get("route"), "path", None)
    if route:
        return str(route)
    # Middleware can run before routing on exceptional paths; redact UUIDs and
    # numeric identifiers rather than exposing resource IDs in logs/metrics.
    return re.sub(r"[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}", ":id", re.sub(r"/\d+(?=/|$)", "/:id", request.url.path))

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = PROJECT_ROOT / "frontend"

@asynccontextmanager
async def _lifespan(app_instance):
    # Production startup fails closed if core durable dependencies cannot be reached.
    if os.getenv("APP_ENV", "development").lower() == "production":
        from db.session import engine
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            try:
                version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            except Exception as exc:
                raise RuntimeError("Production database migrations are not applied.") from exc
            from alembic.script import ScriptDirectory
            from alembic.config import Config
            alembic_cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
            expected = ScriptDirectory.from_config(alembic_cfg).get_current_head()
            if version != expected:
                raise RuntimeError("Production database schema is not at the current migration head.")
        try:
            from jobs.queue import _client
            _client().ping()
        except Exception as exc:
            raise RuntimeError("Production Redis dependency is unavailable.") from exc
    yield
    try:
        from db.session import engine
        engine.dispose()
    except Exception:
        pass

MAX_REQUEST_BODY_BYTES=int(os.getenv("HEALTHGUARD_MAX_REQUEST_BODY_BYTES",str(22*1024*1024)))
app = FastAPI(
    title="HealthGuard API",
    description="Personal health continuity platform with secure patient records and the existing analytics workspace.",
    version="3.0.0",
    lifespan=_lifespan,
)
# Innermost middleware: enforces the body limit on the bytes actually received (see backend/body_limit.py).
app.add_middleware(BodySizeLimitMiddleware, max_bytes=MAX_REQUEST_BODY_BYTES)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request:Request, exc:RequestValidationError):
    return JSONResponse({"detail":"Invalid request."},status_code=422)

@app.exception_handler(IntegrityError)
async def integrity_error_handler(request:Request, exc:IntegrityError):
    logger.warning("Database integrity constraint rejected request path=%s",_observed_route(request))
    return JSONResponse({"detail":"The request conflicts with existing data."},status_code=409)

def _error_classification(status_code:int, exc:Exception|None=None)->str:
    if status_code in {400, 409, 413, 422}: return "validation"
    if status_code in {401,403}: return "authorization"
    if status_code==429: return "rate_limit"
    if status_code in {502,503,504}: return "dependency"
    return "internal" if status_code>=500 else "none"

@app.exception_handler(Exception)
async def unhandled_error_handler(request:Request, exc:Exception):
    request_id=getattr(request.state,"request_id",uuid.uuid4().hex)
    logger.exception("Unhandled API error", extra={"request_id":request_id,"route":_observed_route(request),"method":request.method,"status":500,"error_type":type(exc).__name__,"component":"api","service":"healthguard-api"})
    inc("healthguard_requests_errors_total", status="500", error_class="internal", route=_observed_route(request))
    return JSONResponse({"detail":"An unexpected server error occurred.","request_id":request_id},status_code=500,headers={"X-Request-ID":request_id})

@app.middleware("http")
async def request_observability(request: Request, call_next):
    request_id=request.headers.get("X-Request-ID") or uuid.uuid4().hex
    correlation_id=request.headers.get("X-Correlation-ID") or request_id
    add_gauge("healthguard_requests_in_flight", 1)
    request.state.request_id=request_id; request.state.correlation_id=correlation_id
    started=time.monotonic()
    try:
        response=await call_next(request)
    except Exception as exc:
        elapsed_ms=round((time.monotonic()-started)*1000,2)
        inc("healthguard_requests_total", method=request.method, route=_observed_route(request), status="500")
        observe("healthguard_request_latency", elapsed_ms, method=request.method, route=_observed_route(request))
        logger.exception("request_failed", extra={"request_id":request_id,"correlation_id":correlation_id,"route":_observed_route(request),"method":request.method,"status":500,"latency_ms":elapsed_ms,"error_type":type(exc).__name__,"component":"api","service":"healthguard-api"})
        add_gauge("healthguard_requests_in_flight", -1)
        raise
    elapsed_ms=round((time.monotonic()-started)*1000,2)
    status=str(response.status_code)
    inc("healthguard_requests_total", method=request.method, route=_observed_route(request), status=status)
    if response.status_code>=400:
        inc("healthguard_requests_errors_total", status=status, error_class=_error_classification(response.status_code), route=_observed_route(request))
    observe("healthguard_request_latency", elapsed_ms, method=request.method, route=_observed_route(request))
    response.headers["X-Request-ID"]=request_id
    response.headers["X-Correlation-ID"]=correlation_id
    logger.info("request_completed", extra={"request_id":request_id,"correlation_id":correlation_id,"method":request.method,"route":_observed_route(request),"status":response.status_code,"latency_ms":elapsed_ms,"component":"api","service":"healthguard-api"})
    add_gauge("healthguard_requests_in_flight", -1)
    return response

@app.middleware("http")
async def request_security(request: Request, call_next):
    if request.method in {"POST","PUT","PATCH","DELETE"} and request.cookies.get(auth.SESSION_COOKIE) and not request.headers.get("Authorization"):
        expected=request.cookies.get(auth.CSRF_COOKIE)
        supplied=request.headers.get("X-CSRF-Token")
        if not expected or not supplied or not hmac.compare_digest(expected,supplied):
            return JSONResponse({"detail":"CSRF validation failed."},status_code=403)
    content_length=request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length)>MAX_REQUEST_BODY_BYTES:
                return JSONResponse({"detail":"Request body is too large."},status_code=413)
        except ValueError:
            return JSONResponse({"detail":"Invalid request."},status_code=400)
    response=await call_next(request)
    response.headers.setdefault("X-Content-Type-Options","nosniff")
    response.headers.setdefault("X-Frame-Options","DENY")
    response.headers.setdefault("Referrer-Policy","no-referrer")
    response.headers.setdefault("Permissions-Policy","camera=(), microphone=(), geolocation=()")
    response.headers.setdefault("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
    if os.getenv("APP_ENV","development").lower()=="production":
        response.headers.setdefault("Strict-Transport-Security","max-age=31536000; includeSubDomains")
    response.headers.setdefault("Cache-Control","no-store") if request.url.path.startswith("/api/") else None
    return response

@app.middleware("http")
async def security_event_recorder(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/") and response.status_code in (401, 403, 429) and not (request.url.path.startswith("/api/admin") and response.status_code == 403):
        from domain import security_events
        security_events.record(status=response.status_code, route=security_events.route_label(request), method=request.method,
                               ip=request.client.host if request.client else None, request_id=getattr(request.state, "request_id", None))
    return response

app.include_router(healthguard_router)
app.include_router(measurements_router)
app.include_router(consumer_router)
app.include_router(admin_router)
app.include_router(reminders_router)
app.include_router(privacy_router)

# FastAPI serves the frontend itself, so the deployed app is same-origin and
# CORS stays restricted. CORS_ALLOWED_ORIGINS (comma-separated) can allow
# additional origins, e.g. a separately hosted frontend; it defaults to the
# local-dev origins only.
_env_name=os.getenv("APP_ENV","development").lower()
_default_origins = "http://localhost:8000,http://127.0.0.1:8000"
_allowed_origins = [o.strip().rstrip("/") for o in os.environ.get("CORS_ALLOWED_ORIGINS", _default_origins).split(",") if o.strip()]

def _validate_cors_origins(origins):
    from urllib.parse import urlparse
    for origin in origins:
        if origin == "*":
            raise RuntimeError("Wildcard CORS origins are not permitted with credentialed authentication.")
        parsed=urlparse(origin)
        if parsed.scheme not in {"http","https"} or not parsed.netloc or parsed.path not in {"","/"} or parsed.query or parsed.fragment:
            raise RuntimeError("CORS_ALLOWED_ORIGINS contains an invalid origin.")
        if os.getenv("APP_ENV","development").lower() == "production":
            if parsed.scheme != "https":
                raise RuntimeError("Production CORS origins must use HTTPS.")
            if parsed.hostname in {"localhost","127.0.0.1","::1"} or (parsed.hostname or "").endswith((".localhost",".local")):
                raise RuntimeError("Production CORS origins cannot use development hostnames.")

def _validate_production_config():
    if os.getenv("APP_ENV","development").lower() != "production":
        return
    required = {
        "DATABASE_URL": os.getenv("DATABASE_URL"),
        "REDIS_URL": os.getenv("REDIS_URL"),
        "STORAGE_BACKEND": os.getenv("STORAGE_BACKEND"),
        "STORAGE_ENDPOINT": os.getenv("STORAGE_ENDPOINT"),
        "STORAGE_BUCKET": os.getenv("STORAGE_BUCKET"),
        "STORAGE_ACCESS_KEY": os.getenv("STORAGE_ACCESS_KEY"),
        "STORAGE_SECRET_KEY": os.getenv("STORAGE_SECRET_KEY"),
        "SMTP_HOST": os.getenv("SMTP_HOST"),
        "SMTP_FROM": os.getenv("SMTP_FROM"),
        "HEALTHGUARD_LLM_PROVIDER": os.getenv("HEALTHGUARD_LLM_PROVIDER"),
        "HEALTHGUARD_LLM_BASE_URL": os.getenv("HEALTHGUARD_LLM_BASE_URL"),
        "HEALTHGUARD_LLM_MODEL": os.getenv("HEALTHGUARD_LLM_MODEL"),
        "HEALTHGUARD_LLM_API_KEY": os.getenv("HEALTHGUARD_LLM_API_KEY"),
    }
    missing=[key for key,value in required.items() if not value]
    if missing:
        raise RuntimeError("Missing required production configuration: " + ", ".join(missing))
    if not required["DATABASE_URL"].startswith(("postgresql://","postgresql+psycopg://","postgresql+psycopg2://")):
        raise RuntimeError("Production DATABASE_URL must use PostgreSQL.")
    if required["STORAGE_BACKEND"].lower() != "s3":
        raise RuntimeError("Production STORAGE_BACKEND must be s3.")
    storage_url = urlparse(required["STORAGE_ENDPOINT"])
    if storage_url.scheme != "https" or not storage_url.hostname:
        raise RuntimeError("Production STORAGE_ENDPOINT must use HTTPS.")
    if storage_url.hostname.lower() in {"localhost", "127.0.0.1", "::1"} or storage_url.hostname.lower().endswith((".localhost", ".local")):
        raise RuntimeError("Production STORAGE_ENDPOINT cannot use a development hostname.")
    if os.getenv("DEBUG","false").lower() in {"1","true","yes","on"}:
        raise RuntimeError("DEBUG mode must be disabled in production.")

    redis_url = urlparse(required["REDIS_URL"])
    if redis_url.scheme not in {"redis", "rediss"} or not redis_url.hostname:
        raise RuntimeError("Production REDIS_URL must be a valid redis:// or rediss:// URL.")

    if required["HEALTHGUARD_LLM_PROVIDER"].lower() != "openai_compatible":
        raise RuntimeError("Production HEALTHGUARD_LLM_PROVIDER must be openai_compatible.")
    llm_url = urlparse(required["HEALTHGUARD_LLM_BASE_URL"])
    if llm_url.scheme != "https" or not llm_url.hostname:
        raise RuntimeError("Production HEALTHGUARD_LLM_BASE_URL must use HTTPS.")
    if llm_url.hostname.lower() in {"localhost", "127.0.0.1", "::1"} or llm_url.hostname.lower().endswith((".localhost", ".local")):
        raise RuntimeError("Production HEALTHGUARD_LLM_BASE_URL cannot use a development hostname.")
    ai_timeout = os.getenv("HEALTHGUARD_AI_TIMEOUT_SECONDS")
    ai_retries = os.getenv("HEALTHGUARD_AI_MAX_RETRIES")
    if ai_timeout is None or ai_retries is None:
        raise RuntimeError("Production AI timeout and retry limits must be explicitly configured.")
    try:
        if not 5 <= int(ai_timeout) <= 120:
            raise ValueError
        if not 0 <= int(ai_retries) <= 2:
            raise ValueError
    except ValueError as exc:
        raise RuntimeError("Production AI timeout/retry limits are invalid.") from exc
    embedding_provider = os.getenv("HEALTHGUARD_EMBEDDING_PROVIDER", "").strip().lower()
    if embedding_provider != "openai_compatible":
        raise RuntimeError("Production embedding provider must be an explicit openai_compatible provider; deterministic embeddings are test-only.")
    for key in ("HEALTHGUARD_EMBEDDING_BASE_URL", "HEALTHGUARD_EMBEDDING_API_KEY", "HEALTHGUARD_EMBEDDING_MODEL"):
        if not os.getenv(key):
            raise RuntimeError(f"Missing required production configuration: {key}")

    if os.getenv("SMTP_STARTTLS", "true").lower() not in {"true", "1", "yes", "on"}:
        raise RuntimeError("SMTP_STARTTLS must be enabled in production.")
    from utils.production_config import validate_secrets, validate_smtp   # placeholder secrets; one SMTP check shared with the worker
    validate_secrets()
    validate_smtp()
    metrics_token = os.getenv("HEALTHGUARD_METRICS_TOKEN")
    if metrics_token is not None and len(metrics_token) < 32:
        raise RuntimeError("HEALTHGUARD_METRICS_TOKEN must be at least 32 characters in production.")
    metrics_dir_value = os.getenv("PROMETHEUS_MULTIPROC_DIR")
    if metrics_dir_value and not Path(metrics_dir_value).is_absolute():
        raise RuntimeError("PROMETHEUS_MULTIPROC_DIR must be an absolute path in production.")
    public_base_url()

if _env_name == "production" and not os.environ.get("CORS_ALLOWED_ORIGINS"):
    raise RuntimeError("CORS_ALLOWED_ORIGINS must be explicitly configured in production.")
_validate_cors_origins(_allowed_origins)
_validate_production_config()

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["GET","POST","PATCH","DELETE","OPTIONS"],
    allow_headers=["Authorization","Content-Type","Accept","X-CSRF-Token"],
)


# ── Request/response schemas ────────────────────────────────────────────

class AnalyzeRequest(BaseModel):
    feedback_text: str = Field(..., min_length=1, max_length=5000)
    department: Optional[str] = Field(default=None, max_length=100)
    overall_rating: Optional[int] = Field(default=None, ge=1, le=5)


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=40)
    email: str = Field(..., min_length=3, max_length=120)
    password: str = Field(..., min_length=8, max_length=128)


class LoginRequest(BaseModel):
    username: str
    password: str


class ResendVerificationRequest(BaseModel):
    email: EmailStr


# ── Auth routes (public) ─────────────────────────────────────────────────

@app.post("/api/auth/register",status_code=202)
def register(payload: RegisterRequest, response: Response, db=Depends(get_db), _rl=Depends(rate_limit("register", 10))):
    # Registration intentionally uses one response shape/status for both new and
    # already-registered identifiers to avoid account enumeration.
    try:
        db_user=auth.register_user(db, payload.username, payload.email, payload.password)
    except auth.AuthError:
        db.rollback()
        return {"message":"If the supplied details can be registered, a verification email has been sent.","verification_required":True}
    token=auth.issue_verification_token(db,db_user)
    audit(db, db_user, "ACCOUNT_REGISTERED", "user", db_user.id)
    db.commit()
    try:
        send_verification_email(db_user,token)
    except Exception:
        # The account remains safely unverified; resend can retry delivery.
        return {"message":"If the supplied details can be registered, a verification email has been sent.","verification_required":True}
    return {"message":"If the supplied details can be registered, a verification email has been sent.","verification_required":True}

@app.post("/api/auth/login")
def login(payload: LoginRequest, response: Response, db=Depends(get_db), _rl=Depends(rate_limit("login", 10))):
    try:
        db_user=auth.authenticate_user(db, payload.username, payload.password)
    except auth.AuthError:
        # Do not reveal whether an identifier exists or whether it is merely
        # unverified. All authentication failures use the same public response.
        raise HTTPException(status_code=401, detail="Incorrect username/email or password.")
    audit(db, db_user, "LOGIN", "user", db_user.id)
    db.commit()
    token=auth.create_access_token(db_user)
    auth.set_session_cookies(response,token)
    return {"user":auth.serialize_user(db_user)}

@app.get("/api/auth/verify-email")
def verify_email(token:str, response:Response, db=Depends(get_db)):
    try:
        user=auth.verify_email_token(db,token)
        db.commit()
    except auth.VerificationError as exc:
        db.rollback()
        raise HTTPException(status_code=400,detail=str(exc))
    return RedirectResponse(url="/login?verified=1",status_code=303)

@app.post("/api/auth/resend-verification",status_code=202)
def resend_verification(payload:ResendVerificationRequest, db=Depends(get_db), _rl=Depends(rate_limit("verification_resend", 3))):
    # Always return the same response regardless of account state to avoid account enumeration.
    user=db.scalar(__import__("sqlalchemy").select(User).where(User.email==str(payload.email).lower()))
    if user and user.status==__import__("db.models",fromlist=["AccountStatus"]).AccountStatus.ACTIVE and not user.email_verified:
        try:
            token=auth.issue_verification_token(db,user)
            db.commit()
            send_verification_email(user,token)
        except Exception:
            db.rollback()
    return {"message":"If the account can receive verification mail, a new verification link has been sent."}

@app.post("/api/auth/logout",status_code=204)
def logout(response:Response, current_user: User = Depends(auth.get_current_user), db=Depends(get_db)):
    current_user.session_version += 1
    audit(db, current_user, "LOGOUT", "user", current_user.id)
    db.commit()
    auth.clear_session_cookies(response)

@app.get("/api/auth/me")
def me(current_user: User = Depends(auth.get_current_user)):
    return auth.serialize_user(current_user)


# ── Public status ────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {"status":"ok","service":"healthguard-api","version":app.version}

@app.get("/api/ready")
def readiness():
    """Bounded dependency readiness; never exposes connection details."""
    checks={"database":False,"redis":False,"storage":True}
    try:
        from db.session import engine
        with engine.connect() as conn: conn.execute(text("SELECT 1"))
        checks["database"]=True
    except Exception as exc:
        inc("healthguard_dependency_failures_total", dependency="database", error_type=type(exc).__name__)
    try:
        from jobs.queue import _client, _close
        client=_client()
        try: client.ping(); checks["redis"]=True
        finally: _close(client)
    except Exception as exc:
        inc("healthguard_dependency_failures_total", dependency="redis", error_type=type(exc).__name__)
    try:
        from storage.service import storage
        checks["storage"]=storage.health_check()
    except Exception as exc:
        checks["storage"]=False
        inc("healthguard_dependency_failures_total", dependency="storage", error_type=type(exc).__name__)
    ready=all(checks.values()) if os.getenv("APP_ENV","development").lower()=="production" else checks["database"]
    return JSONResponse({"status":"ready" if ready else "not_ready","checks":checks},status_code=200 if ready else 503)

@app.get("/api/ops/metrics")
def operational_metrics(x_metrics_token: str | None = Header(default=None, alias="X-Metrics-Token")):
    configured=os.getenv("HEALTHGUARD_METRICS_TOKEN")
    if os.getenv("APP_ENV","development").lower()=="production":
        if not configured or not x_metrics_token or not hmac.compare_digest(x_metrics_token, configured):
            raise HTTPException(status_code=403, detail="Operational metrics access denied.")
    elif configured and (not x_metrics_token or not hmac.compare_digest(x_metrics_token, configured)):
        raise HTTPException(status_code=403, detail="Operational metrics access denied.")
    return Response(content=prometheus(), media_type="text/plain; version=0.0.4")


# ── Protected workspace routes ───────────────────────────────────────────

@app.post("/api/analyze")
def analyze(payload: AnalyzeRequest, current_user: User = Depends(auth.get_current_user)):
    from backend import pipeline
    """Analyze a single piece of feedback using the persisted ML models
    (issue classifier + satisfaction model) with rule-based fallback where
    applicable. No retraining occurs on this or any request."""
    try:
        result = pipeline.analyze_single_feedback(
            feedback_text=payload.feedback_text,
            department=payload.department,
            overall_rating=payload.overall_rating,
        )
        return result
    except pipeline.PipelineError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        logger.exception("Unexpected error in /api/analyze")
        raise HTTPException(status_code=500, detail="Analysis failed unexpectedly.")


@app.post("/api/upload")
async def upload(file: UploadFile = File(...), current_user: User = Depends(auth.get_current_user), _rl=Depends(rate_limit("upload", 30))):
    from backend import pipeline
    """Upload a patient-survey CSV and run it through the full existing
    pipeline (clean -> preprocess -> feature_engineer -> sentiment ->
    categorize_issues -> priority_scorer -> action_mapper -> insights)."""
    try:
        max_bytes = MAX_REQUEST_BODY_BYTES
        chunks = []
        total = 0
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise HTTPException(status_code=413, detail="Request body is too large.")
            chunks.append(chunk)
        file_bytes = b"".join(chunks)
        if not file_bytes:
            raise HTTPException(status_code=400, detail="Uploaded file is empty.")
        result = pipeline.run_full_pipeline(file.filename or "upload.csv", file_bytes, owner_id=str(current_user.id))
        return result
    except pipeline.PipelineError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error in /api/upload")
        raise HTTPException(status_code=500, detail="Upload processing failed unexpectedly.")


@app.get("/api/metrics")
def get_metrics(upload_id: Optional[str] = None, current_user: User = Depends(auth.get_current_user)):
    from backend import pipeline
    from backend import metrics as metrics_module
    """Dashboard payload for a given upload_id, or the most recent upload
    when omitted. Also includes the persisted ML model training metrics
    (read from models/*/metrics.json — not recomputed here)."""
    try:
        record = pipeline.get_upload(upload_id, owner_id=str(current_user.id))
    except pipeline.PipelineError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    dashboard = metrics_module.build_dashboard_metrics(record)
    dashboard["model_metrics"] = metrics_module.build_model_metrics()
    dashboard["model_status"] = pipeline.get_model_status()
    return JSONResponse(content=_jsonable(dashboard))


@app.get("/api/uploads")
def uploads_list(current_user: User = Depends(auth.get_current_user)):
    """Convenience endpoint (not in the required list, but harmless/useful)
    listing all uploads held in the in-memory store this process session."""
    from backend import pipeline
    return pipeline.list_uploads(owner_id=str(current_user.id))


def _jsonable(obj):
    """Recursively coerce numpy/pandas scalar types to plain Python types
    so FastAPI's default JSON encoder never chokes on them."""
    import numpy as np
    import pandas as pd

    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return None if pd.isna(obj) else float(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if pd.isna(obj) if not isinstance(obj, (list, dict)) else False:
        return None
    return obj


# ── Static frontend ──────────────────────────────────────────────────────
# Serves the frontend directly from FastAPI so the whole app is a single
# process to run. Page-level "auth protection" for the workspace pages is
# enforced client-side (frontend/js/workspace.js redirects to /login when
# there's no valid token) — the actual data is protected server-side by the
# JWT-guarded API routes above, so nothing sensitive is reachable without a
# valid token regardless of which HTML happens to load.

if FRONTEND_DIR.exists():
    app.mount("/css", StaticFiles(directory=FRONTEND_DIR / "css"), name="css")
    app.mount("/js", StaticFiles(directory=FRONTEND_DIR / "js"), name="js")

    def _serve(filename: str):
        return FileResponse(FRONTEND_DIR / filename)

    @app.get("/")
    def serve_index():
        return _serve("index.html")

    @app.get("/login")
    def serve_login():
        return _serve("login.html")

    @app.get("/register")
    def serve_register():
        return _serve("register.html")

    @app.get("/verify")
    def serve_verify():
        return _serve("verify.html")

    @app.get("/workspace")
    def serve_workspace():
        return _serve("workspace.html")

    @app.get("/settings")
    def serve_settings():
        return _serve("settings.html")

    @app.get("/handoff")
    def serve_handoff():
        return _serve("handoff.html")

    @app.get("/actions")
    def serve_actions():
        return _serve("actions.html")

    @app.get("/health")
    def serve_health():
        return _serve("health.html")

    @app.get("/patient")
    def serve_patient():
        return _serve("patient.html")

    @app.get("/admin")
    def serve_admin():
        return _serve("admin.html")

    @app.get("/hub")
    def serve_hub():
        return _serve("hub.html")


    @app.get("/analyze")
    def serve_analyze():
        return _serve("analyze.html")

    @app.get("/insights")
    def serve_insights():
        return _serve("insights.html")

    @app.get("/departments")
    def serve_departments():
        return _serve("departments.html")

    @app.get("/models")
    def serve_models():
        return _serve("models.html")

    # Old URLs kept as redirects so existing links/bookmarks don't 404.
    @app.get("/dashboard.html")
    def legacy_dashboard():
        return RedirectResponse(url="/workspace")

    @app.get("/analyze.html")
    def legacy_analyze():
        return RedirectResponse(url="/analyze")

_SAMPLE_DATA_DIR = PROJECT_ROOT / "data"
if _SAMPLE_DATA_DIR.exists():
    # Lets the workspace's "Load Sample Dataset" button fetch the same
    # synthetic development CSV training/generate_sample_data.py produced,
    # then POST it to /api/upload like any other user-provided file.
    app.mount("/sample-data", StaticFiles(directory=_SAMPLE_DATA_DIR), name="sample-data")
