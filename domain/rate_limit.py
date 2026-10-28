from __future__ import annotations
import hashlib
import os
import time
import threading
from fastapi import Request, HTTPException
from starlette.concurrency import run_in_threadpool
try:
    import redis
except ImportError:
    redis = None

_lock = threading.Lock()
_local = {}
WINDOW = 60
LIMIT = int(os.getenv("HEALTHGUARD_RATE_LIMIT_PER_MINUTE", "60"))


def _client_ip(request: Request) -> str:
    # Never trust forwarded headers unless a trusted-proxy configuration is
    # explicitly introduced. Starlette's direct peer address is the only source
    # used here, so an attacker cannot choose the bucket via X-Forwarded-For.
    return request.client.host if request.client else "unknown"


def _authenticated_identity(request: Request) -> str | None:
    # Rate limiting must not become an authorization dependency. When a valid
    # bearer/session JWT is present, use its verified subject only to isolate
    # abuse buckets. Invalid tokens remain anonymous and are handled by auth.
    headers = getattr(request, "headers", {}) or {}
    cookies = getattr(request, "cookies", {}) or {}
    token = headers.get("Authorization", "")
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    else:
        token = cookies.get("hg_session")
    if not token:
        return None
    try:
        from backend.auth import decode_access_token
        subject = decode_access_token(token)
        return hashlib.sha256(subject.encode("utf-8")).hexdigest()[:32]
    except Exception:
        return None


_clients: dict = {}
_clients_lock = threading.Lock()


def _redis_timeouts() -> tuple[float, float]:
    # Read at call time so operators/tests can tune them; both are hard upper bounds on how long one request can wait.
    return (float(os.getenv("HEALTHGUARD_REDIS_CONNECT_TIMEOUT_SECONDS", "0.5")), float(os.getenv("HEALTHGUARD_REDIS_SOCKET_TIMEOUT_SECONDS", "0.5")))


def _redis_client(url: str):
    """One pooled client per (url, timeouts). Creating a client per request leaked connections and, with no socket
    timeout, let a single hung Redis stall every request."""
    connect, sock = _redis_timeouts()
    key = (url, connect, sock)
    with _clients_lock:
        client = _clients.get(key)
        if client is None:
            client = redis.from_url(url, socket_connect_timeout=connect, socket_timeout=sock, health_check_interval=30)
            _clients[key] = client
        return client


def _redis_hit(url: str, key: str) -> int:
    """Blocking Redis work; always run in a worker thread. INCR and TTL go in one MULTI/EXEC so the counter's TTL is
    observed atomically, and a counter that somehow has no TTL (crash between INCR and EXPIRE) is repaired here."""
    r = _redis_client(url)
    pipe = r.pipeline()
    pipe.incr(key); pipe.ttl(key)
    count, ttl = pipe.execute()
    if count == 1 or ttl is None or ttl < 0:
        r.expire(key, WINDOW)
    return int(count)


def rate_limit(bucket: str, limit: int = LIMIT):
    async def dep(request: Request):
        identity = _authenticated_identity(request)
        ident = _client_ip(request)
        # Endpoint bucket + peer IP + authenticated identity prevents a single
        # authenticated user from trivially shifting limits across endpoints or
        # identities while retaining anonymous IP-based protection.
        key = f"healthguard:rl:{bucket}:{ident}:{identity or 'anonymous'}"
        rurl = os.getenv("REDIS_URL")
        production = os.getenv("APP_ENV", "development").lower() == "production"

        if production and not rurl:
            raise HTTPException(503, "Rate limiting service is temporarily unavailable.")
        if rurl and redis:
            try:
                count = await run_in_threadpool(_redis_hit, rurl, key)   # never block the event loop on network I/O
            except Exception as exc:
                if production:
                    raise HTTPException(503, "Rate limiting service is temporarily unavailable.") from exc
            else:
                if count > limit:
                    raise HTTPException(429, "Too many requests. Please try again shortly.")
                return
        elif production:
            # Production must never silently fall back to process-local limits;
            # multiple workers would otherwise create independent buckets.
            raise HTTPException(503, "Rate limiting service is temporarily unavailable.")

        now = time.monotonic()
        with _lock:
            started, count = _local.get(key, (now, 0))
            if now - started >= WINDOW:
                started, count = now, 0
            count += 1
            _local[key] = (started, count)
            if count > limit:
                raise HTTPException(429, "Too many requests. Please try again shortly.")
    return dep
