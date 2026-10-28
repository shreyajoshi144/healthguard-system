"""Real-browser verification (Chromium, real uvicorn, migrated SQLite) of the privacy/consent/export journey and accessibility.

Accessibility scope, stated honestly: axe-core automated rules plus explicit keyboard checks. Automated checks find only a
subset of WCAG problems; this is NOT a WCAG conformance audit and no assistive-technology (screen reader) testing was done.
"""
from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

try:
    from playwright.sync_api import sync_playwright
except Exception:                                            # pragma: no cover
    sync_playwright = None

ROOT = Path(__file__).resolve().parents[2]
AXE = (ROOT / "tests" / "vendor" / "axe.min.js").read_text(encoding="utf-8")
SURVEY = os.getenv("HG_A11Y_SURVEY") == "1"
BLOCKING_IMPACTS = {"serious", "critical"}


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


@pytest.fixture
def app(tmp_path):
    if sync_playwright is None:
        pytest.fail("Playwright is not installed: browser verification cannot be skipped silently")
    port = _free_port(); base = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env.update({"APP_ENV": "test", "DATABASE_URL": f"sqlite:///{tmp_path}/a11y.db", "STORAGE_BACKEND": "local", "STORAGE_LOCAL_ROOT": str(tmp_path / "private"),
                "HEALTHGUARD_JWT_SECRET": "x" * 64, "REDIS_URL": "", "CORS_ALLOWED_ORIGINS": base, "HEALTHGUARD_PUBLIC_BASE_URL": base,
                "HEALTHGUARD_LLM_API_KEY": "test-key", "PYTHONPATH": str(ROOT)})
    mig = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT, env=env, capture_output=True, text=True)
    assert mig.returncode == 0, mig.stderr
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from backend.auth import _hash_password
    from db.models import AccountStatus, PatientProfile, User
    from tests.p0.test_consumer import rich
    db = sessionmaker(bind=create_engine(env["DATABASE_URL"]))()
    owner = User(username="privowner", email="privowner@example.test", password_hash=_hash_password("OwnerPass123!"), email_verified=True, status=AccountStatus.ACTIVE)
    db.add(owner); db.flush()
    pat = PatientProfile(owner_user_id=owner.id, first_name="Asha", last_name="Verma"); db.add(pat); db.flush()
    rich(db, owner, pat); pid = str(pat.id); db.close()
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", str(port)], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try: socket.create_connection(("127.0.0.1", port), timeout=.2).close(); break
            except OSError: time.sleep(.2)
        else: raise AssertionError("server did not start")
        yield base, pid
    finally:
        proc.terminate(); proc.wait(10)


def _browser(p):
    return p.chromium.launch(headless=True, args=["--no-sandbox", "--no-proxy-server"], executable_path=os.environ.get("HEALTHGUARD_CHROMIUM_PATH") or None)


def _login(page, base):
    page.goto(f"{base}/login"); page.locator("#username").fill("privowner"); page.locator("#password").fill("OwnerPass123!")
    page.locator("#loginForm button[type=submit]").click(); page.wait_for_url("**/workspace")


def _axe(page):
    page.evaluate(AXE)
    return page.evaluate("async () => (await axe.run(document, {runOnly: {type: 'tag', values: ['wcag2a','wcag2aa','wcag21a','wcag21aa','best-practice']}})).violations"
                         ".map(v => ({id: v.id, impact: v.impact, help: v.help, nodes: v.nodes.map(n => n.target.join(' ')).slice(0, 3)}))")


def test_consent_and_export_journey_in_a_real_browser(app, tmp_path):
    base, pid = app
    with sync_playwright() as p:
        browser = _browser(p); errors: list[str] = []
        page = browser.new_context(accept_downloads=True).new_page(); page.set_default_timeout(15000)
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        _login(page, base); page.goto(f"{base}/settings")
        box = page.locator("#aiConsent"); box.wait_for()
        assert not box.is_checked() and "Policy version 1" in page.locator("#aiConsentNote").inner_text()
        box.check()
        page.get_by_text("AI processing is allowed.").wait_for()
        page.reload(); page.locator("#aiConsent").wait_for(); assert page.locator("#aiConsent").is_checked(), "consent must persist across reloads"
        # export
        with page.expect_download() as d:
            page.get_by_role("button", name="Download Asha Verma as JSON").click()
        data = json.loads(Path(d.value.path()).read_text(encoding="utf-8"))
        assert data["schema_version"] == "healthguard.export.v1" and data["patient"]["first_name"] == "Asha" and data["counts"]["health_events"] >= 1
        assert "password_hash" not in json.dumps(data) and "storage_key" not in json.dumps(data)
        with page.expect_download() as d2:
            page.get_by_role("button", name="Download Asha Verma as ZIP").click()
        assert "healthguard-export.json" in zipfile.ZipFile(io.BytesIO(Path(d2.value.path()).read_bytes())).namelist()
        page.get_by_text("downloaded.").first.wait_for()
        # withdraw
        page.locator("#aiConsent").uncheck(); page.get_by_text("AI processing is turned off.").wait_for()
        assert not errors, errors
        browser.close()


def test_privacy_controls_work_with_the_keyboard_alone_and_show_a_visible_focus(app):
    base, pid = app
    with sync_playwright() as p:
        browser = _browser(p); page = browser.new_context().new_page(); page.set_default_timeout(15000)
        _login(page, base); page.goto(f"{base}/settings"); page.locator("#aiConsent").wait_for()
        reached = False
        for _ in range(40):                                              # tab until the consent checkbox has focus
            page.keyboard.press("Tab")
            if page.evaluate("document.activeElement && document.activeElement.id") == "aiConsent": reached = True; break
        assert reached, "consent checkbox is not reachable by keyboard"
        outline = page.evaluate("(() => { const s = getComputedStyle(document.activeElement); return [s.outlineStyle, parseFloat(s.outlineWidth)]; })()")
        assert outline[0] != "none" and outline[1] >= 2, f"no visible focus indicator on the checkbox: {outline}"
        page.keyboard.press("Space"); page.get_by_text("AI processing is allowed.").wait_for()
        assert page.locator("#aiConsent").is_checked() and page.evaluate("document.activeElement.id") == "aiConsent", "focus must stay on the control after it changes"
        for _ in range(8):                                               # tab on to an export button and activate it with Enter
            page.keyboard.press("Tab")
            if "Download Asha Verma as JSON" in (page.evaluate("document.activeElement.getAttribute('aria-label')") or ""): break
        else: raise AssertionError("export button not reachable by keyboard")
        with page.expect_download():
            page.keyboard.press("Enter")
        browser.close()


PAGES = ["/login", "/register", "/workspace", "/settings", "/hub?patient_id={pid}", "/patient?id={pid}"]


def test_automated_accessibility_scan_has_no_serious_or_critical_violations(app):
    base, pid = app
    report: dict[str, list] = {}
    with sync_playwright() as p:
        browser = _browser(p); page = browser.new_context().new_page(); page.set_default_timeout(15000)
        for path in PAGES[:2]:
            page.goto(base + path); page.wait_for_load_state("networkidle"); report[path] = _axe(page)
        _login(page, base)
        for path in PAGES[2:]:
            page.goto(base + path.format(pid=pid)); page.wait_for_load_state("networkidle"); time.sleep(0.5); report[path] = _axe(page)
        browser.close()
    if SURVEY:
        print("\nA11Y SURVEY"); [print(f"  {pg}: {[(v['id'], v['impact'], v['nodes'][:1]) for v in vs]}") for pg, vs in report.items()]
    blocking = {pg: [v for v in vs if v["impact"] in BLOCKING_IMPACTS] for pg, vs in report.items()}
    blocking = {pg: vs for pg, vs in blocking.items() if vs}
    assert not blocking, f"serious/critical accessibility violations: {json.dumps(blocking, indent=1)}"
