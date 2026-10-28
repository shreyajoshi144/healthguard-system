"""P0 real-browser journey (Chromium via Playwright, real uvicorn server, migrated SQLite DB).
Covers: hub overview/summary/ask/questions/medications/vitals/timeline/graph/visit/follow-ups/family,
caregiver scope enforcement + revocation in a second browser session, and the admin console with TOTP step-up.
Run with PLAYWRIGHT_BROWSERS_PATH pointing at an installed Chromium."""
from __future__ import annotations
import os, re, socket, subprocess, sys, time, uuid
from pathlib import Path
import pytest
try:
    from playwright.sync_api import sync_playwright, expect
except Exception:
    sync_playwright = None

ROOT = Path(__file__).resolve().parents[2]
KEY = "e" * 48

def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

@pytest.mark.skipif(sync_playwright is None, reason="Playwright package unavailable")
def test_p0_hub_and_admin_browser_journey(tmp_path, monkeypatch):
    port = _free_port(); base = f"http://127.0.0.1:{port}"
    env = os.environ.copy(); env.update({"APP_ENV": "test", "DATABASE_URL": f"sqlite:///{tmp_path}/p0.db", "STORAGE_BACKEND": "local", "STORAGE_LOCAL_ROOT": str(tmp_path / "private"),
        "HEALTHGUARD_JWT_SECRET": "x" * 64, "REDIS_URL": "", "CORS_ALLOWED_ORIGINS": base, "HEALTHGUARD_PUBLIC_BASE_URL": base, "HEALTHGUARD_ADMIN_TOTP_KEY": KEY, "PYTHONPATH": str(ROOT)})
    mig = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT, env=env, capture_output=True, text=True)
    assert mig.returncode == 0, mig.stderr
    monkeypatch.setenv("HEALTHGUARD_ADMIN_TOTP_KEY", KEY)

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from db.models import User, AccountStatus, PatientProfile, AdminGrant, AdminRole
    from backend.auth import _hash_password
    from domain import admin_ops
    from tests.p0.test_consumer import rich
    S = sessionmaker(bind=create_engine(env["DATABASE_URL"]))
    db = S()
    owner = User(username="e2eowner", email="e2eowner@example.test", password_hash=_hash_password("OwnerPass123!"), email_verified=True, status=AccountStatus.ACTIVE)
    fam = User(username="e2efamily", email="e2efamily@example.com", password_hash=_hash_password("FamilyPass123!"), email_verified=True, status=AccountStatus.ACTIVE)
    db.add_all([owner, fam]); db.flush()
    pat = PatientProfile(owner_user_id=owner.id, first_name="Asha", last_name="Verma"); db.add(pat); db.flush()
    grant = AdminGrant(user_id=owner.id, role=AdminRole.OPERATOR, totp_salt="e2e-salt"); db.add(grant); db.commit()
    rich(db, owner, pat); pid = str(pat.id); db.close()

    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", str(port)], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=.2).close(); break
            except OSError: time.sleep(.2)
        else: raise AssertionError("server did not start")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--no-proxy-server"], executable_path=os.environ.get("HEALTHGUARD_CHROMIUM_PATH") or None)
            errors: list[str] = []
            def watch(page):
                page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
                page.on("console", lambda m: errors.append(f"console: {m.text}") if m.type == "error" and "Failed to load resource" not in m.text else None)
            def login(page, user, pw):
                page.goto(f"{base}/login"); page.locator("#username").fill(user); page.locator("#password").fill(pw)
                page.locator("#loginForm button[type=submit]").click(); page.wait_for_url("**/workspace")
            ctx = browser.new_context(); page = ctx.new_page(); watch(page); page.set_default_timeout(15000)
            login(page, "e2eowner", "OwnerPass123!")

            # ---- overview ----
            page.goto(f"{base}/hub?patient_id={pid}")
            expect(page.locator("#ctx")).to_contain_text("You own this profile")
            expect(page.locator("#view")).to_contain_text("Asha Verma"); expect(page.locator("#view")).to_contain_text("Metformin")
            expect(page.locator("#view")).to_contain_text("Aspirin"); expect(page.locator("#view")).to_contain_text("unconfirmed")
            expect(page.locator("#view")).to_contain_text("suggested, awaiting your confirmation"); expect(page.locator("#view")).to_contain_text("past its date")
            expect(page.locator("#view")).to_contain_text("disagreement(s) between records")
            assert "LEAKED-DELETED-DOC" not in page.content() and "deleted.pdf" not in page.content()
            # ---- summary ----
            page.locator(".hub-nav").get_by_role("link", name="Health summary").click()
            expect(page.locator("#view")).to_contain_text("Conditions documented in your records")
            expect(page.locator("#view a.chip").first).to_be_visible()
            href = page.locator("#view a.chip").first.get_attribute("href"); assert href.startswith("/api/documents/") and href.endswith("/content")
            n_short = page.locator("#view li.row").count()
            page.get_by_role("button", name="Detailed").click(); expect(page.locator("#view")).to_contain_text("Medication changes")
            assert page.locator("#view li.row").count() > n_short
            expect(page.locator("#view")).to_contain_text("system-derived")
            # ---- ask (no AI provider configured here -> must degrade gracefully, never invent an answer) ----
            page.locator(".hub-nav").get_by_role("link", name="Ask my records").click()
            page.locator("#qn").fill("When was my last HbA1c test?"); page.get_by_role("button", name="Ask", exact=True).click()
            expect(page.locator("#out")).to_contain_text(re.compile("not available right now|not enough evidence|Answer"))
            assert "7.9" not in page.locator("#out").inner_text() or "Evidence cited" in page.locator("#out").inner_text()
            # ---- doctor questions ----
            page.locator(".hub-nav").get_by_role("link", name="Questions for my doctor").click()
            expect(page.locator("#view")).to_contain_text("What your records document"); expect(page.locator("#view")).to_contain_text("Uncertain or conflicting"); expect(page.locator("#view")).to_contain_text("Suggested questions")
            expect(page.locator("#view")).to_contain_text("Which is correct and current?")
            # ---- medications ----
            page.locator(".hub-nav").get_by_role("link", name="Medications", exact=True).click()
            expect(page.locator("#view")).to_contain_text("not confirmed medication history")
            page.locator("#view summary", has_text="Add a medication").click(); page.locator("#addmed input[name=medicine_name]").fill("Atorvastatin"); page.locator("#addmed select[name=status]").select_option("ACTIVE")
            page.locator("#addmed button").click(); expect(page.locator("#view")).to_contain_text("Atorvastatin")
            # ---- vitals ----
            page.locator(".hub-nav").get_by_role("link", name="Vitals").click(); expect(page.locator("#view")).to_contain_text("Blood pressure")
            expect(page.locator("#view svg.chart")).to_be_visible()
            page.locator("#view summary", has_text="Add a blood pressure reading").click()
            page.locator("#mf input[name=value]").fill("500"); page.locator("#mf input[name=value_secondary]").fill("80"); page.locator("#mf button").click()
            expect(page.locator(".err-msg")).to_contain_text("plausible")
            page.locator("#mf input[name=value]").fill("128"); page.locator("#mf button").click(); expect(page.locator(".ok-msg")).to_contain_text("Reading saved")
            expect(page.locator("#view")).to_contain_text("128/80 mmHg")
            page.locator(".tabs").get_by_role("link", name="Heart rate").click(); expect(page.locator("#view")).to_contain_text("No heart rate readings yet")
            # ---- timeline ----
            page.locator(".hub-nav").get_by_role("link", name="Timeline").click(); expect(page.locator("#view")).to_contain_text("Date unknown")
            expect(page.locator("#view")).to_contain_text("upload date, not the clinical date"); n_all = page.locator("#items .it").count()
            page.locator("#flt a", has_text="Vitals").click(); deadline = time.time() + 10
            while time.time() < deadline and not (0 < page.locator("#items .it").count() < n_all): time.sleep(.2)
            assert 0 < page.locator("#items .it").count() < n_all, "type filter did not narrow the timeline"
            # ---- graph ----
            page.locator(".hub-nav").get_by_role("link", name="Health graph").click(); expect(page.locator("svg.graph")).to_be_visible()
            assert page.locator("svg.graph g.n").count() >= 8
            page.locator("svg.graph g.n", has_text="Metformin").first.click(); expect(page.locator("#detail")).to_contain_text("evidence")
            # ---- visit prep ----
            page.locator(".hub-nav").get_by_role("link", name="Doctor visit prep").click(); page.locator("#reason").fill("Second opinion"); expect(page.locator("#reasonout")).to_contain_text("Second opinion")
            expect(page.locator("#view")).to_contain_text("Questions to ask")
            # ---- follow-ups ----
            page.locator(".hub-nav").get_by_role("link", name="Follow-ups").click()
            expect(page.locator("#view")).to_contain_text("suggested · not confirmed"); page.locator("[data-act=confirm]").first.click()
            expect(page.locator("#view")).to_contain_text("confirmed by you")
            page.locator("#view summary", has_text="Add a follow-up").click(); page.locator("#af input[name=title]").fill("Call clinic"); page.locator("#af button").click()
            expect(page.locator("#view")).to_contain_text("does not invent deadlines")
            # ---- email reminders: preferences, scheduling, status, cancellation (delivery itself is covered by tests/p0/test_reminders.py) ----
            from datetime import date, timedelta
            page.locator("#view summary", has_text="Add a follow-up").click(); page.locator("#af input[name=title]").fill("Dated visit"); page.locator("#af input[name=due_date]").fill((date.today() + timedelta(days=5)).isoformat()); page.locator("#af button").click()
            expect(page.locator("#view")).to_contain_text("Dated visit")
            expect(page.locator("#rembody")).to_contain_text("Email only"); expect(page.locator("#rembody")).to_contain_text("Email delivery is not configured")
            page.locator("#rp input[name=enabled]").check(); page.locator("#rp input[name=lead][value='1']").check(); page.locator("#rp input[name=lead][value='7']").check()
            page.locator("#rp button").click(); expect(page.locator(".ok-msg")).to_contain_text("Reminders are on")
            expect(page.locator("#rembody")).to_contain_text("Dated visit"); expect(page.locator("#rembody")).to_contain_text("scheduled"); assert "Call clinic" not in page.locator("#rembody table").inner_text()   # undated follow-up never reminded
            page.locator("#rp input[name=lead][value='0']").check(); page.locator("#rp input[name=lead][value='2']").check(); page.locator("#rp input[name=lead][value='3']").check()
            page.locator("#rp button").click(); expect(page.locator(".err-msg")).to_contain_text("check the options")             # 5 reminder times exceeds the maximum of 4
            page.reload(); expect(page.locator("#rp input[name=enabled]")).to_be_checked()
            page.locator("#rp input[name=enabled]").uncheck(); page.locator("#rp button").click(); expect(page.locator(".ok-msg")).to_contain_text("Reminders are off")
            expect(page.locator("#rembody")).to_contain_text("cancelled")
            # ---- family: grant caregiver (view meds only) ----
            page.locator(".hub-nav").get_by_role("link", name="Family & sharing").click()
            page.locator("#view summary", has_text="Give someone access").click(); page.locator("#rf input[name=user_email]").fill("e2efamily@example.com"); page.locator("#rf select[name=role]").select_option("CAREGIVER")
            page.locator("#rf input[name=scopes][value=MEDICATIONS]").check(); page.locator("#rf button").click()
            expect(page.locator("#view")).to_contain_text("e2efamily@example.com")
            # ---- second session: caregiver sees only granted scopes ----
            ctx2 = browser.new_context(); p2 = ctx2.new_page(); watch(p2); p2.set_default_timeout(15000); login(p2, "e2efamily", "FamilyPass123!")
            p2.goto(f"{base}/hub?patient_id={pid}"); expect(p2.locator("#ctx")).to_contain_text("Shared with you as caregiver")
            expect(p2.locator("#view")).to_contain_text("Metformin"); assert "lab-2024.pdf" not in p2.content() and "HbA1c" not in p2.locator("#view").inner_text()
            expect(p2.locator("#view")).to_contain_text("outside your access")
            p2.locator(".hub-nav").get_by_role("link", name="Ask my records").click(); expect(p2.locator("#view")).to_contain_text("outside the access granted")
            p2.goto(f"{base}/admin"); expect(p2.locator("#view")).to_contain_text("not available")                 # non-admin sees nothing of the console
            # ---- owner revokes; caregiver loses access immediately ----
            page.once("dialog", lambda d: d.accept()); page.locator("[data-rev]").first.click(); expect(page.locator("#view")).to_contain_text("Nobody else has ongoing access")
            p2.goto(f"{base}/hub?patient_id={pid}"); expect(p2.locator("#view")).to_contain_text("no patient profiles")
            # ---- documents page lists a FAILED document and offers recovery ----
            page.goto(f"{base}/patient?id={pid}")
            expect(page.locator("body")).to_contain_text("rx.pdf"); expect(page.locator("[data-retry]").first).to_be_visible()
            # ---- admin console with TOTP step-up ----
            page.goto(f"{base}/admin"); expect(page.locator("#view")).to_contain_text("Administrator verification required")
            page.locator("input[name=code]").fill("000000"); page.get_by_role("button", name="Verify").click(); expect(page.locator("#msg")).to_contain_text("Invalid")
            page.locator("input[name=code]").fill(admin_ops.totp_now("e2e-salt")); page.get_by_role("button", name="Verify").click()
            expect(page.locator("#view")).to_contain_text("Overview"); expect(page.locator("#view")).to_contain_text("Migrations"); expect(page.locator("#view")).to_contain_text("Not verified by this build")
            expect(page.locator("#who")).to_contain_text("operator")
            page.locator(".hub-nav").get_by_role("link", name="Jobs & processing").click(); expect(page.locator("#view")).to_contain_text("opaque id")
            page.locator(".hub-nav").get_by_role("link", name="AI operations").click(); expect(page.locator("#view")).to_contain_text(re.compile("no cost is reported|does not record token"))
            page.locator(".hub-nav").get_by_role("link", name="Security", exact=True).click(); expect(page.locator("#view")).to_contain_text("Privileged admin actions")
            page.locator(".hub-nav").get_by_role("link", name="Users").click(); expect(page.locator("#view")).to_contain_text(re.compile(r"e\*\*\*@example\.test")); assert "e2eowner@example.test" not in page.locator("#view").inner_text()
            page.locator(".hub-nav").get_by_role("link", name="Alerts").click(); expect(page.locator("#view")).to_contain_text("No external alert channel")
            page.locator(".hub-nav").get_by_role("link", name="Deployment & release").click(); expect(page.locator("#view")).to_contain_text("never exposed")
            body = page.locator("body").inner_text()
            for phi in ("Metformin", "HbA1c", "lab-2024.pdf", "Asha", "Verma", KEY, "x" * 64): assert phi not in body, phi
            # ---- responsive (mobile) layout renders and nothing overflows horizontally ----
            ctx3 = browser.new_context(viewport={"width": 390, "height": 800}); p3 = ctx3.new_page(); watch(p3); p3.set_default_timeout(15000); login(p3, "e2eowner", "OwnerPass123!")
            p3.goto(f"{base}/hub?patient_id={pid}"); expect(p3.locator("#view")).to_contain_text("Metformin")
            assert p3.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), "horizontal overflow on mobile"
            assert not errors, errors
            browser.close()
    finally:
        proc.terminate()
        try: proc.wait(10)
        except Exception: proc.kill()
