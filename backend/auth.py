from __future__ import annotations
import hashlib, hmac, os, secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID
import jwt
from fastapi import Cookie, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session
from db.models import User, AccountStatus, EmailVerificationToken
from db.session import get_db
from utils.logger import get_logger

logger=get_logger(__name__)
SECRET_KEY=os.environ.get("HEALTHGUARD_JWT_SECRET")
if not SECRET_KEY:
    if os.getenv("APP_ENV","development").lower()=="production":
        raise RuntimeError("HEALTHGUARD_JWT_SECRET must be set in production.")
    SECRET_KEY=secrets.token_hex(32)
if len(SECRET_KEY)<32:
    raise RuntimeError("HEALTHGUARD_JWT_SECRET must be at least 32 characters.")
ALGORITHM="HS256"
TOKEN_EXPIRY_HOURS=int(os.environ.get("HEALTHGUARD_TOKEN_EXPIRY_HOURS","8"))
VERIFICATION_EXPIRY_HOURS=int(os.environ.get("HEALTHGUARD_VERIFICATION_EXPIRY_HOURS","24"))
SESSION_COOKIE="hg_session"
CSRF_COOKIE="hg_csrf"
oauth2_scheme=OAuth2PasswordBearer(tokenUrl="/api/auth/login",auto_error=False)

class AuthError(Exception): pass
class VerificationError(Exception): pass

PBKDF2_ITERATIONS=int(os.getenv("HEALTHGUARD_PBKDF2_ITERATIONS","600000"))
def _hash_password(password:str)->str:
    salt=secrets.token_bytes(16)
    digest=hashlib.pbkdf2_hmac("sha256",password.encode(),salt,PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"

def _verify_password(password:str,password_hash:str)->bool:
    try:
        scheme,iterations,salt_hex,digest_hex=password_hash.split("$",3)
        if scheme!="pbkdf2_sha256": return False
        candidate=hashlib.pbkdf2_hmac("sha256",password.encode(),bytes.fromhex(salt_hex),int(iterations))
        return hmac.compare_digest(candidate.hex(),digest_hex)
    except (ValueError,TypeError): return False

def _token_hash(raw_token:str)->str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

def register_user(db:Session, username:str,email:str,password:str)->User:
    username=username.strip(); email=email.strip().lower()
    if len(username)<3 or len(username)>40 or len(password)<8:
        raise AuthError("Unable to create the account with the supplied details.")
    if db.scalar(select(User).where(func.lower(User.username)==username.lower())) or db.scalar(select(User).where(User.email==email)):
        raise AuthError("Unable to create the account with the supplied details.")
    user=User(username=username,email=email,password_hash=_hash_password(password),email_verified=False)
    db.add(user); db.flush()
    return user

def authenticate_user(db:Session, username_or_email:str,password:str)->User:
    key=username_or_email.strip().lower()
    user=db.scalar(select(User).where((func.lower(User.username)==key) | (User.email==key)))   # exact match: never a LIKE pattern
    if not user or user.status!=AccountStatus.ACTIVE or not _verify_password(password,user.password_hash):
        raise AuthError("Incorrect username/email or password.")
    if not user.email_verified:
        raise AuthError("Please verify your email before signing in.")
    return user

def serialize_user(user:User)->dict:
    return {"id":str(user.id),"username":user.username,"email":user.email,"email_verified":user.email_verified,"status":user.status.value}

def create_access_token(user:User|str)->str:
    sub=str(user.id) if isinstance(user,User) else str(user)
    expire=datetime.now(timezone.utc)+timedelta(hours=TOKEN_EXPIRY_HOURS)
    version = user.session_version if isinstance(user, User) else None
    claims={"sub":sub,"exp":expire}
    if version is not None:
        claims["sv"]=int(version)
    return jwt.encode(claims,SECRET_KEY,algorithm=ALGORITHM)

def decode_access_token(token:str)->str:
    try: payload=jwt.decode(token,SECRET_KEY,algorithms=[ALGORITHM],options={"require":["exp","sub"]})
    except jwt.ExpiredSignatureError: raise AuthError("Session expired — please log in again.")
    except jwt.InvalidTokenError: raise AuthError("Invalid authentication token.")
    sub=payload.get("sub")
    if not sub: raise AuthError("Invalid authentication token.")
    return str(sub)

def _decode_session_version(token:str)->int|None:
    try:
        payload=jwt.decode(token,SECRET_KEY,algorithms=[ALGORITHM])
    except jwt.InvalidTokenError:
        return None
    value=payload.get("sv")
    return int(value) if value is not None else None

def issue_verification_token(db:Session,user:User)->str:
    # Revoke outstanding tokens before issuing a fresh one; only the newest link is valid.
    now=datetime.now(timezone.utc)
    for old in db.scalars(select(EmailVerificationToken).where(EmailVerificationToken.user_id==user.id,EmailVerificationToken.used_at.is_(None))).all():
        old.used_at=now
    raw=secrets.token_urlsafe(32)
    db.add(EmailVerificationToken(user_id=user.id,token_hash=_token_hash(raw),expires_at=now+timedelta(hours=VERIFICATION_EXPIRY_HOURS)))
    db.flush()
    return raw

def verify_email_token(db:Session,raw_token:str)->User:
    token_hash=_token_hash(raw_token)
    now=datetime.now(timezone.utc)
    # Atomically consume the token. This prevents two concurrent requests from
    # both successfully replaying a one-time verification token.
    result=db.execute(
        update(EmailVerificationToken)
        .where(
            EmailVerificationToken.token_hash==token_hash,
            EmailVerificationToken.used_at.is_(None),
            EmailVerificationToken.expires_at>now,
        )
        .values(used_at=now)
    )
    if result.rowcount != 1:
        raise VerificationError("Invalid or expired verification link.")
    row=db.scalar(select(EmailVerificationToken).where(EmailVerificationToken.token_hash==token_hash))
    user=db.get(User,row.user_id) if row else None
    if not user or user.status!=AccountStatus.ACTIVE:
        raise VerificationError("Invalid or expired verification link.")
    user.email_verified=True
    return user

def get_current_user(request:Request, token:str|None=Depends(oauth2_scheme), session_cookie:str|None=Cookie(default=None,alias=SESSION_COOKIE), db:Session=Depends(get_db))->User:
    token=token or session_cookie
    if not token:
        raise HTTPException(status_code=401,detail="Not authenticated.",headers={"WWW-Authenticate":"Bearer"})
    try:
        user_id=decode_access_token(token)
        user=db.get(User,UUID(user_id))
        token_version=_decode_session_version(token)
        if user is not None and (token_version is None or int(token_version) != int(user.session_version)):
            user=None
    except (AuthError,ValueError):
        user=None
    if not user or user.status!=AccountStatus.ACTIVE or not user.email_verified:
        raise HTTPException(status_code=401,detail="Invalid authentication token.",headers={"WWW-Authenticate":"Bearer"})
    return user

def set_session_cookies(response,token:str)->str:
    secure=os.getenv("APP_ENV","development").lower()=="production"
    csrf=secrets.token_urlsafe(24)
    response.set_cookie(SESSION_COOKIE,token,max_age=TOKEN_EXPIRY_HOURS*3600,httponly=True,secure=secure,samesite="lax",path="/")
    response.set_cookie(CSRF_COOKIE,csrf,max_age=TOKEN_EXPIRY_HOURS*3600,httponly=False,secure=secure,samesite="lax",path="/")
    return csrf

def clear_session_cookies(response)->None:
    response.delete_cookie(SESSION_COOKIE,path="/")
    response.delete_cookie(CSRF_COOKIE,path="/")
