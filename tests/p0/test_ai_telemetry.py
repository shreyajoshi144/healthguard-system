"""AI provider telemetry: tokens, served model, latency, failures and (only if priced) cost, without PHI. Uses a real local HTTP server."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.request import Request

import pytest

from ai import provider_utils
from domain.admin_ops import ai_metrics
from domain.observability import prometheus


class _Provider:
    """Minimal OpenAI-compatible endpoint whose response body is chosen per test."""
    def __init__(self): self.reply, self.status, self.calls = {}, 200, 0

    def start(self):
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                outer.calls += 1; self.rfile.read(int(self.headers.get("content-length", 0)))
                self.send_response(outer.status); self.send_header("content-type", "application/json"); self.end_headers()
                self.wfile.write(json.dumps(outer.reply).encode())
            def log_message(self, *a): pass
        self.srv = HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}/v1/chat/completions"; return self

    def stop(self): self.srv.shutdown()


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("HEALTHGUARD_AI_MAX_RETRIES", "0")
    p = _Provider().start(); yield p; p.stop()


def _call(url, model="m-alias"):
    req = Request(url, data=json.dumps({"model": model, "messages": [{"role": "user", "content": "Patient Asha Verma has hemoglobin 14.2"}]}).encode(), headers={"content-type": "application/json"})
    return provider_utils.post_json(req)


def _metric(name, **flt):
    from domain.admin_ops import _parse_prometheus
    return sum(v for n, l, v in _parse_prometheus(prometheus()) if n == name and all(l.get(k) == x for k, x in flt.items()))


def test_reported_token_usage_and_served_model_are_recorded(provider):
    model = "tele-model-a"
    b_p, b_c = _metric("healthguard_ai_tokens_total", model=model, kind="prompt"), _metric("healthguard_ai_tokens_total", model=model, kind="completion")
    provider.reply = {"model": "tele-model-a-2026-01-01", "choices": [], "usage": {"prompt_tokens": 120, "completion_tokens": 35, "total_tokens": 155}}
    _call(provider.url, model)
    assert _metric("healthguard_ai_tokens_total", model=model, kind="prompt") - b_p == 120
    assert _metric("healthguard_ai_tokens_total", model=model, kind="completion") - b_c == 35
    assert _metric("healthguard_ai_served_model_total", requested=model, served="tele-model-a-2026-01-01") >= 1


def test_missing_usage_is_counted_not_invented(provider):
    model = "tele-model-b"; before = _metric("healthguard_ai_usage_missing_total", model=model)
    provider.reply = {"model": model, "choices": []}
    _call(provider.url, model)
    assert _metric("healthguard_ai_usage_missing_total", model=model) - before == 1
    assert _metric("healthguard_ai_tokens_total", model=model, kind="prompt") == 0


@pytest.mark.parametrize("usage", [{"prompt_tokens": -5, "completion_tokens": 1}, {"prompt_tokens": "9999999999999", "completion_tokens": 1}, {"prompt_tokens": 1e30, "completion_tokens": 1},
                                   {"prompt_tokens": True, "completion_tokens": None}, "not-a-dict", [1, 2]])
def test_hostile_or_malformed_usage_is_ignored_safely(provider, usage):
    model = "tele-model-c"; before = _metric("healthguard_ai_tokens_total", model=model, kind="prompt")
    provider.reply = {"model": model, "choices": [], "usage": usage}
    assert _call(provider.url, model)["model"] == model                      # the call itself still succeeds
    assert _metric("healthguard_ai_tokens_total", model=model, kind="prompt") == before


def test_cost_is_computed_only_from_operator_configured_prices(provider, monkeypatch):
    model = "tele-model-d"; provider.reply = {"model": model, "choices": [], "usage": {"prompt_tokens": 2000, "completion_tokens": 1000}}
    monkeypatch.delenv("HEALTHGUARD_AI_PRICE_PROMPT_PER_1K", raising=False); monkeypatch.delenv("HEALTHGUARD_AI_PRICE_COMPLETION_PER_1K", raising=False)
    _call(provider.url, model)
    assert _metric("healthguard_ai_cost_micro_total", model=model) == 0, "no prices configured -> no cost may be reported"
    monkeypatch.setenv("HEALTHGUARD_AI_PRICE_PROMPT_PER_1K", "0.5"); monkeypatch.setenv("HEALTHGUARD_AI_PRICE_COMPLETION_PER_1K", "1.5")
    _call(provider.url, model)
    assert _metric("healthguard_ai_cost_micro_total", model=model) == int((2000 / 1000 * 0.5 + 1000 / 1000 * 1.5) * 1_000_000)


def test_admin_summary_reports_tokens_and_marks_cost_unavailable_without_prices(provider, monkeypatch):
    monkeypatch.delenv("HEALTHGUARD_AI_PRICE_PROMPT_PER_1K", raising=False); monkeypatch.delenv("HEALTHGUARD_AI_PRICE_COMPLETION_PER_1K", raising=False)
    provider.reply = {"model": "tele-model-e", "choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
    _call(provider.url, "tele-model-e")
    t = ai_metrics()["tokens_and_cost"]
    assert t["available"] is True and t["tokens"]["prompt"] >= 10 and t["tokens"]["completion"] >= 5
    assert t["cost"]["available"] is False and "price" in t["cost"]["reason"].lower()


def test_failures_are_recorded_and_no_phi_reaches_metric_labels(provider):
    provider.status = 500; provider.reply = {"error": "boom"}
    with pytest.raises(Exception): _call(provider.url, "tele-model-f")
    text = prometheus()
    assert 'model="tele-model-f"' in text and "Asha" not in text and "hemoglobin" not in text.lower() and "Verma" not in text
