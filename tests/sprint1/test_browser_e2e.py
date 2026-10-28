from __future__ import annotations
import os, socket, subprocess, sys, time
from pathlib import Path
import pytest
from sqlalchemy import select

try:
    from playwright.sync_api import sync_playwright
except Exception:
    sync_playwright = None

ROOT = Path(__file__).resolve().parents[2]


def _free_port():
    s=socket.socket(); s.bind(("127.0.0.1",0)); port=s.getsockname()[1]; s.close(); return port

@pytest.mark.skipif(sync_playwright is None, reason="Playwright package unavailable")
def test_healthguard_browser_sprint1_journey(tmp_path):
    import socketserver, threading, email, re
    class SMTPHandler(socketserver.StreamRequestHandler):
        server_version="HealthGuardTestSMTP"
        def handle(self):
            self.wfile.write(b"220 test.smtp ESMTP\r\n")
            data_mode=False; buf=[]
            while True:
                line=self.rfile.readline()
                if not line: break
                text=line.decode("utf-8", errors="replace").rstrip("\r\n")
                if data_mode:
                    if text==".":
                        self.server.messages.append("\n".join(buf)); buf=[]; data_mode=False; self.wfile.write(b"250 OK\r\n")
                    else: buf.append(text[1:] if text.startswith("..") else text)
                    continue
                cmd=text.upper()
                if cmd.startswith("EHLO") or cmd.startswith("HELO"): self.wfile.write(b"250-test.smtp\r\n250 OK\r\n")
                elif cmd.startswith("MAIL FROM") or cmd.startswith("RCPT TO"): self.wfile.write(b"250 OK\r\n")
                elif cmd=="DATA": data_mode=True; self.wfile.write(b"354 End data with <CR><LF>.<CR><LF>\r\n")
                elif cmd=="QUIT": self.wfile.write(b"221 Bye\r\n"); break
                else: self.wfile.write(b"250 OK\r\n")
    class CaptureSMTP(socketserver.ThreadingTCPServer):
        allow_reuse_address=True
        def __init__(self, addr): super().__init__(addr, SMTPHandler); self.messages=[]
    smtp_port=_free_port(); smtp=CaptureSMTP(("127.0.0.1",smtp_port))
    thread=threading.Thread(target=smtp.serve_forever,daemon=True); thread.start()
    db_path=tmp_path/"browser.db"
    port=_free_port()
    env=os.environ.copy(); env.update({
        "HEALTHGUARD_PUBLIC_BASE_URL":f"http://127.0.0.1:{port}",
        "APP_ENV":"test", "DATABASE_URL":f"sqlite:///{db_path}",
        "STORAGE_BACKEND":"local", "STORAGE_LOCAL_ROOT":str(tmp_path/"private"),
        "HEALTHGUARD_JWT_SECRET":"x"*64, "REDIS_URL":"", "CORS_ALLOWED_ORIGINS":"http://127.0.0.1:0",
        "SMTP_HOST":"127.0.0.1", "SMTP_PORT":str(smtp_port), "SMTP_FROM":"healthguard@example.test", "SMTP_STARTTLS":"false",
        "PYTHONPATH":str(ROOT),
    })
    migration=subprocess.run([sys.executable,"-m","alembic","upgrade","head"],cwd=ROOT,env=env,capture_output=True,text=True)
    assert migration.returncode == 0, f"Test database migration failed:\nSTDOUT: {migration.stdout}\nSTDERR: {migration.stderr}"
    proc=subprocess.Popen([sys.executable,"-m","uvicorn","backend.main:app","--host","127.0.0.1","--port",str(port)],cwd=ROOT,env=env,stdout=open(tmp_path/"server.log","w"),stderr=subprocess.STDOUT,text=True)   # a never-read PIPE blocks the server once full
    try:
        deadline=time.time()+20
        while time.time()<deadline:
            try:
                with socket.create_connection(("127.0.0.1",port),timeout=.2): break
            except OSError: time.sleep(.2)
        else: raise AssertionError("API did not start")
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True, args=["--no-sandbox", "--no-proxy-server"], executable_path=os.environ.get("HEALTHGUARD_CHROMIUM_PATH") or None)
            page=browser.new_page()
            try:
                page.goto(f"http://127.0.0.1:{port}/register")
            except Exception:
                raise
            page.locator("#username").fill("browserowner")
            page.locator("#email").fill("browserowner@example.test")
            page.locator("#password").fill("StrongPass123!")
            page.locator("#registerForm button:not([type=button])").click()
            page.wait_for_url("**/verify?email=browserowner%40example.test")
            deadline=time.time()+5
            while not smtp.messages and time.time()<deadline: time.sleep(.1)
            assert smtp.messages, "verification email was not captured"
            body=email.message_from_string(smtp.messages[-1]).get_payload(decode=True).decode()     # message is quoted-printable encoded
            match=re.search(r"http://127\.0\.0\.1:%d/api/auth/verify-email\?token=([^\s]+)" % port, body)
            assert match, body
            page.goto(match.group(0))
            page.wait_for_url("**/login?verified=1")
            page.locator("#username").fill("browserowner"); page.locator("#password").fill("StrongPass123!"); page.locator("#loginForm button:not([type=button])").click()
            page.wait_for_url("**/workspace"); assert page.locator("h1").inner_text()=="My Health"
            page.reload(); page.wait_for_selector("text=Patient profiles")
            page.locator("a[href=\"/health\"]").first.click(); page.wait_for_url("**/health"); page.wait_for_selector("#addPatient")   # patient creation lives on /health
            page.locator("#addPatient").click(); page.locator("#first").fill("Browser"); page.locator("#last").fill("Patient"); page.locator("#patientForm button:not([type=button])").click(); page.wait_for_selector("text=Browser Patient")
            page.locator("text=Open record").click(); page.wait_for_url("**/patient?id=*")
            page.locator("#addEvent").click(); page.locator("#event_title").fill("Historical consultation"); page.locator("#eventForm button:not([type=button])").click(); page.wait_for_selector("text=Historical consultation")
            page.locator("#addMed").click(); page.locator("#medicine_name").fill("Historical Medicine"); page.locator("#medForm button:not([type=button])").click(); page.wait_for_selector("text=Historical Medicine")
            page.locator("[data-med-event]").click(); page.locator("#medEventForm button:not([type=button])").click(); page.wait_for_timeout(150)
            page.locator("#addInv").click(); page.locator("#inv_name").fill("Blood Test"); page.locator("#invForm button:not([type=button])").click(); page.wait_for_selector("text=Blood Test")
            page.locator("[data-result]").click(); page.locator("#result_summary").fill("Result recorded by user"); page.locator("#resultForm button:not([type=button])").click(); page.wait_for_timeout(150)
            patient_url=page.url; patient_id=__import__("uuid").UUID(patient_url.split("id=")[-1])
            # Document upload and recovery retry. Force the persisted job to FAILED, then recover it through the UI.
            test_file=tmp_path/"record.txt"; test_file.write_bytes(b"not a valid medical document")
            # Use a minimal valid PNG instead so content validation passes.
            png=tmp_path/"record.png"; png.write_bytes(bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360000000020001e221bc330000000049454e44ae426082"))
            page.locator("#doc").set_input_files(str(png)); page.wait_for_timeout(500);
            from sqlalchemy import create_engine; from sqlalchemy.orm import sessionmaker; SessionLocal=sessionmaker(bind=create_engine(env["DATABASE_URL"]))   # the server's DB, not the process default
            from db.models import Document, ProcessingJob, DocumentStatus
            db=SessionLocal(); d=db.scalar(select(Document).where(Document.patient_id==patient_id)); d.status=DocumentStatus.FAILED; j=db.scalar(select(ProcessingJob).where(ProcessingJob.document_id==d.id)); j.status=DocumentStatus.FAILED; db.commit(); db.close()
            page.reload(); page.wait_for_selector("text=FAILED"); page.locator("[data-retry]").click(); page.wait_for_selector("text=QUEUED")
            # Create a second verified user directly for the family-access portion; the UI path itself is exercised for grant/revoke.
            db=SessionLocal();
            from db.models import User, AccountStatus
            from backend.auth import _hash_password
            family=User(username="browserfamily",email="browserfamily@example.com",password_hash=_hash_password("FamilyPass123!"),email_verified=True,status=AccountStatus.ACTIVE); db.add(family); db.commit(); db.refresh(family); family_email=family.email; db.close()
            page.locator("#addFamily").click(); page.locator("#family_email").fill(family_email); page.locator("#familyForm button:not([type=button])").click(); page.wait_for_selector("text=browserfamily@example.com")
            page.goto(page.url.split("/patient")[0] + "/settings"); page.wait_for_selector("text=Session security")
            page.locator("[data-logout]").click(); page.wait_for_url("**/login")
            # Family member login and patient access, then revoke from owner.
            page.locator("#username").fill("browserfamily"); page.locator("#password").fill("FamilyPass123!"); page.locator("#loginForm button:not([type=button])").click(); page.wait_for_url("**/workspace"); page.goto(f"http://127.0.0.1:{port}/health"); page.wait_for_selector("text=Browser Patient");
            page.locator("[data-logout]").click(); page.wait_for_url("**/login"); page.locator("#username").fill("browserowner"); page.locator("#password").fill("StrongPass123!"); page.locator("#loginForm button:not([type=button])").click(); page.wait_for_url("**/workspace"); page.goto(patient_url); page.wait_for_selector("text=browserfamily@example.com"); page.once("dialog", lambda d:d.accept()); page.locator("[data-revoke]").click(); page.wait_for_timeout(200)
            # Revoke click may be intercepted by confirm; invoke via DOM with confirmation accepted.
            page.reload(); page.wait_for_timeout(150);
            page.locator("[data-logout]").click(); page.wait_for_url("**/login")
            page.locator("#username").fill("browserfamily"); page.locator("#password").fill("FamilyPass123!"); page.locator("#loginForm button:not([type=button])").click(); page.wait_for_url("**/workspace"); assert page.request.get(f"http://127.0.0.1:{port}/api/patients/{patient_id}").status == 403            # revoked caregiver is refused by the server
            assert page.request.get(f"http://127.0.0.1:{port}/api/patients/{patient_id}/documents").status == 403
            page.goto(f"http://127.0.0.1:{port}/patient?id={patient_id}"); page.wait_for_timeout(500); assert "Browser Patient" not in page.locator("body").inner_text()
            page.locator("[data-logout]").click(); page.wait_for_url("**/login")
            page.goto(f"http://127.0.0.1:{port}/workspace"); page.wait_for_url("**/login?next=%2Fworkspace")
            browser.close()
    finally:
        smtp.shutdown(); smtp.server_close(); proc.terminate()
        try: proc.wait(timeout=5)
        except subprocess.TimeoutExpired: proc.kill()
