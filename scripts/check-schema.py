#!/usr/bin/env python3
from pathlib import Path
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
import os

root = Path(__file__).resolve().parents[1]
url = os.environ.get("DATABASE_URL")
if not url:
    raise SystemExit("DATABASE_URL is required")
engine = create_engine(url, pool_pre_ping=True)
cfg = Config(str(root / "alembic.ini"))
expected = ScriptDirectory.from_config(cfg).get_current_head()
with engine.connect() as conn:
    try:
        current = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    except Exception as exc:
        raise SystemExit("Production database migrations are not applied.") from exc
if current != expected:
    raise SystemExit("Production database schema is not at the current migration head.")
print(f"Database schema is at Alembic head {expected}.")
