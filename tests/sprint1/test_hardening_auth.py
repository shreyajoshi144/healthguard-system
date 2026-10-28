from __future__ import annotations
from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
import pytest

from db.models import Base, User, EmailVerificationToken, AccountStatus
from db.session import get_db
from backend import auth

@pytest.fixture
def db():
    engine=create_engine("sqlite://",connect_args={"check_same_thread":False},poolclass=StaticPool)
    @event.listens_for(engine,"connect")
    def _fk(dbapi_connection, connection_record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    Session=sessionmaker(bind=engine,expire_on_commit=False)
    s=Session()
    try: yield s
    finally: s.close()

def test_password_not_stored_plaintext(db):
    u=auth.register_user(db,"secureuser","secure@example.com","correct horse battery staple")
    assert u.password_hash!="correct horse battery staple"
    assert auth._verify_password("correct horse battery staple",u.password_hash)

def test_registration_creates_unverified_account(monkeypatch,db):
    monkeypatch.setattr("backend.main.send_verification_email",lambda user,token:None)
    from backend.main import app
    app.dependency_overrides[get_db]=lambda:db
    r=TestClient(app).post("/api/auth/register",json={"username":"newuser","email":"new@example.com","password":"correct horse battery staple"})
    assert r.status_code==202
    u=db.scalar(select(User).where(User.email=="new@example.com"))
    assert u and not u.email_verified

def test_valid_verification_token_and_single_use(db):
    u=auth.register_user(db,"verifyuser","verify@example.com","correct horse battery staple")
    raw=auth.issue_verification_token(db,u); db.commit()
    assert not u.email_verified
    auth.verify_email_token(db,raw); db.commit()
    assert u.email_verified
    with pytest.raises(auth.VerificationError): auth.verify_email_token(db,raw)

def test_expired_verification_token(db):
    u=auth.register_user(db,"expireduser","expired@example.com","correct horse battery staple")
    import secrets
    raw=secrets.token_urlsafe(32)
    db.add(EmailVerificationToken(user_id=u.id,token_hash=auth._token_hash(raw),expires_at=datetime.now(timezone.utc)-timedelta(minutes=1)))
    db.commit()
    with pytest.raises(auth.VerificationError): auth.verify_email_token(db,raw)

def test_unverified_login_blocked(db):
    u=auth.register_user(db,"loginuser","login@example.com","correct horse battery staple"); db.commit()
    with pytest.raises(auth.AuthError): auth.authenticate_user(db,"login@example.com","correct horse battery staple")

def test_authentication_rate_limit(monkeypatch):
    from domain.rate_limit import rate_limit
    from fastapi import Request
    import asyncio
    class Client: host="127.0.0.1"
    class R: client=Client()
    dep=rate_limit("auth-test",2)
    asyncio.run(dep(R())); asyncio.run(dep(R()))
    with pytest.raises(Exception) as exc: asyncio.run(dep(R()))
    assert getattr(exc.value,"status_code",None)==429


def test_verification_email_flow_and_replay(monkeypatch,db):
    sent=[]
    monkeypatch.setattr("backend.main.send_verification_email",lambda user,token:sent.append((user.email,token)))
    from backend.main import app
    app.dependency_overrides[get_db]=lambda:db
    c=TestClient(app)
    r=c.post("/api/auth/register",json={"username":"flowuser","email":"flow@example.com","password":"correct horse battery staple"})
    assert r.status_code==202
    assert sent and sent[0][0]=="flow@example.com"
    raw=sent[0][1]
    v=c.get("/api/auth/verify-email",params={"token":raw},follow_redirects=False)
    assert v.status_code==303 and v.headers["location"].endswith("/login?verified=1")
    replay=c.get("/api/auth/verify-email",params={"token":raw})
    assert replay.status_code==400


def test_verification_resend_rate_limited(monkeypatch,db):
    monkeypatch.setattr("backend.main.send_verification_email",lambda user,token:None)
    from backend.main import app
    app.dependency_overrides[get_db]=lambda:db
    c=TestClient(app)
    c.post("/api/auth/register",json={"username":"resenduser","email":"resend@example.com","password":"correct horse battery staple"})
    statuses=[c.post("/api/auth/resend-verification",json={"email":"resend@example.com"}).status_code for _ in range(4)]
    assert statuses[:3]==[202,202,202] and statuses[3]==429


def test_cookie_session_and_csrf_protection(db):
    u=auth.register_user(db,"cookieuser","cookie@example.com","correct horse battery staple"); u.email_verified=True; db.commit()
    from backend.main import app
    app.dependency_overrides[get_db]=lambda:db
    c=TestClient(app)
    r=c.post("/api/auth/login",json={"username":"cookie@example.com","password":"correct horse battery staple"})
    assert r.status_code==200
    cookies=[v for k,v in r.headers.raw if k.lower()==b"set-cookie"]
    assert any(b"hg_session=" in v and b"HttpOnly" in v for v in cookies)
    assert c.get("/api/auth/me").status_code==200
    assert c.post("/api/auth/logout").status_code==403
    csrf=c.cookies.get("hg_csrf")
    assert c.post("/api/auth/logout",headers={"X-CSRF-Token":csrf}).status_code==204


def test_registration_does_not_enumerate_existing_accounts(monkeypatch,db):
    monkeypatch.setattr("backend.main.send_verification_email",lambda user,token:None)
    from backend.main import app
    app.dependency_overrides[get_db]=lambda:db
    c=TestClient(app)
    first=c.post("/api/auth/register",json={"username":"enumuser","email":"enum@example.com","password":"correct horse battery staple"})
    second=c.post("/api/auth/register",json={"username":"enumuser","email":"enum@example.com","password":"correct horse battery staple"})
    assert first.status_code==second.status_code==202
    assert first.json()["message"]==second.json()["message"]
