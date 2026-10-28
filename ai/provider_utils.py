from __future__ import annotations

import re

import json
import os
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from domain.observability import inc, observe

DEFAULT_TIMEOUT = 45
DEFAULT_RETRIES = 1
MAX_TIMEOUT = 120
MAX_RETRIES = 2
MAX_RESPONSE_BYTES = 1_000_000
MAX_PROMPT_CHARS = 120_000
MAX_EVIDENCE_ITEMS = 20
MAX_EVIDENCE_ITEM_CHARS = 12_000
MAX_OUTPUT_CHARS = 12_000


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer.") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}.")
    return value


def provider_timeout() -> int:
    return _bounded_int("HEALTHGUARD_AI_TIMEOUT_SECONDS", DEFAULT_TIMEOUT, 5, MAX_TIMEOUT)


def provider_retries() -> int:
    return _bounded_int("HEALTHGUARD_AI_MAX_RETRIES", DEFAULT_RETRIES, 0, MAX_RETRIES)


def validate_prompt_size(text: str, *, field: str = "AI input") -> None:
    if len(text) > MAX_PROMPT_CHARS:
        raise ValueError(f"{field} exceeds the configured AI input limit.")


def validate_evidence_payload(evidence: list[dict[str, Any]]) -> None:
    if len(evidence) > MAX_EVIDENCE_ITEMS:
        raise ValueError("AI evidence item limit exceeded.")
    total = 0
    for item in evidence:
        encoded = json.dumps(item, default=str, ensure_ascii=False)
        if len(encoded) > MAX_EVIDENCE_ITEM_CHARS:
            raise ValueError("AI evidence item exceeds the configured size limit.")
        total += len(encoded)
    if total > MAX_PROMPT_CHARS:
        raise ValueError("AI evidence payload exceeds the configured size limit.")


def _read_limited(response) -> bytes:
    body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("AI provider response exceeds the configured size limit.")
    return body


_MAX_TOKENS_PER_CALL = 10_000_000


def _token_count(value: Any) -> int | None:
    """Provider-reported usage is untrusted input: only plain, bounded, non-negative integers are accepted."""
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= _MAX_TOKENS_PER_CALL else None


def _price(name: str) -> float | None:
    try:
        v = float(os.getenv(name, ""))
    except ValueError:
        return None
    return v if 0 <= v < 1_000_000 else None


def _record_usage(body: Any, model: str) -> None:
    """Record tokens, the model the provider actually served, and (only if prices are configured) cost. No content, no PHI."""
    if not isinstance(body, dict):
        return
    served = body.get("model")
    if isinstance(served, str) and served:
        inc("healthguard_ai_served_model_total", requested=model, served=re.sub(r"[^A-Za-z0-9._:/-]", "_", served)[:80])
    usage = body.get("usage")
    prompt = _token_count(usage.get("prompt_tokens")) if isinstance(usage, dict) else None
    completion = _token_count(usage.get("completion_tokens")) if isinstance(usage, dict) else None
    if prompt is None or completion is None:
        inc("healthguard_ai_usage_missing_total", model=model)         # counted, never estimated
        return
    inc("healthguard_ai_tokens_total", prompt, model=model, kind="prompt")
    inc("healthguard_ai_tokens_total", completion, model=model, kind="completion")
    p_price, c_price = _price("HEALTHGUARD_AI_PRICE_PROMPT_PER_1K"), _price("HEALTHGUARD_AI_PRICE_COMPLETION_PER_1K")
    if p_price is not None and c_price is not None:
        inc("healthguard_ai_cost_micro_total", int(round((prompt / 1000 * p_price + completion / 1000 * c_price) * 1_000_000)), model=model)


def post_json(request: Request) -> dict[str, Any]:
    retries = provider_retries()
    last_error: Exception | None = None
    model="unknown"
    try:
        parsed=json.loads(request.data.decode("utf-8")) if request.data else {}
        model=str(parsed.get("model") or "unknown")[:80]
    except Exception:
        pass
    started=time.monotonic()
    for attempt in range(retries + 1):
        try:
            with urlopen(request, timeout=provider_timeout()) as response:
                body=json.loads(_read_limited(response).decode("utf-8"))
                _record_usage(body, model)
                inc("healthguard_ai_provider_calls_total", outcome="success", model=model)
                observe("healthguard_ai_provider_latency", (time.monotonic()-started)*1000, model=model)
                return body
        except HTTPError as exc:
            last_error = exc
            inc("healthguard_ai_provider_attempts_total", outcome="http_error", model=model, status=exc.code)
            # Retry only transient/rate-limit responses. Never blindly retry
            # malformed requests or other non-idempotent failures.
            if exc.code not in {408, 429, 500, 502, 503, 504} or attempt >= retries:
                raise
        except (URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            inc("healthguard_ai_provider_attempts_total", outcome="failure", model=model, error_type=type(exc).__name__)
            if attempt >= retries:
                raise
        if attempt < retries:
            time.sleep(min(0.25 * (2**attempt), 1.0))
    inc("healthguard_ai_provider_calls_total", outcome="failure", model=model)
    observe("healthguard_ai_provider_latency", (time.monotonic()-started)*1000, model=model)
    raise RuntimeError("AI provider request failed.") from last_error
