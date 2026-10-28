#!/usr/bin/env python3
"""Offline admin provisioning. There is deliberately NO API for creating admins.

  python scripts/admin.py grant <username> [--role VIEWER|OPERATOR]   # prints the TOTP provisioning URI once
  python scripts/admin.py revoke <username>
  python scripts/admin.py list

Requires DATABASE_URL and HEALTHGUARD_ADMIN_TOTP_KEY (>= 32 chars).
"""
import argparse, secrets, sys
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import select
from db.session import SessionLocal
from db.models import AdminGrant, AdminRole, User, AuditLog
from domain import admin_ops as ops

def main() -> int:
    ap = argparse.ArgumentParser(); sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("grant"); g.add_argument("username"); g.add_argument("--role", choices=[r.value for r in AdminRole], default="VIEWER")
    r = sub.add_parser("revoke"); r.add_argument("username"); sub.add_parser("list")
    a = ap.parse_args()
    if a.cmd in {"grant"} and not ops.totp_key():
        print("HEALTHGUARD_ADMIN_TOTP_KEY (>=32 chars) must be set.", file=sys.stderr); return 2
    db = SessionLocal()
    try:
        if a.cmd == "list":
            for grant, user in db.execute(select(AdminGrant, User).join(User, User.id == AdminGrant.user_id)).all():
                print(f"{user.username}\t{grant.role.value}\t{'REVOKED' if grant.revoked_at else 'ACTIVE'}")
            return 0
        user = db.scalar(select(User).where(User.username == a.username))
        if not user: print("No such user.", file=sys.stderr); return 1
        existing = db.scalar(select(AdminGrant).where(AdminGrant.user_id == user.id))
        if a.cmd == "revoke":
            if not existing or existing.revoked_at: print("No active admin grant.", file=sys.stderr); return 1
            existing.revoked_at = datetime.now(timezone.utc); user.session_version += 1
            db.add(AuditLog(actor_user_id=None, action="ADMIN_GRANT_REVOKED", resource_type="admin_grant", resource_id=str(existing.id))); db.commit(); print("Revoked."); return 0
        if existing and not existing.revoked_at: print("User already has an active admin grant.", file=sys.stderr); return 1
        if existing: db.delete(existing); db.flush()
        grant = AdminGrant(user_id=user.id, role=AdminRole(a.role), totp_salt=secrets.token_hex(16)); db.add(grant); db.flush()
        db.add(AuditLog(actor_user_id=None, action="ADMIN_GRANT_CREATED", resource_type="admin_grant", resource_id=str(grant.id), context={"role": a.role})); db.commit()
        print(f"Granted {a.role} to {user.username}. Add this to an authenticator app (shown once):\n{ops.provisioning_uri(user.username, grant.totp_salt)}")
        return 0
    finally:
        db.close()

if __name__ == "__main__":
    raise SystemExit(main())
