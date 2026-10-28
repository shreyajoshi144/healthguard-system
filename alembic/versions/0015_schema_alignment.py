"""Align PostgreSQL schema with the ORM models (fixes `alembic check` drift).

* documents.status, processing_jobs.status and the provenance columns of health_events, investigations,
  medications and medication_events were created as native PostgreSQL ENUM types, while the models use
  portable VARCHAR enums. Convert the columns to VARCHAR (values preserved) and drop the now-unused types.
* temporary_shares.token_hash carried both a UNIQUE constraint and a unique index; keep only the index.
On SQLite (dev/test) the provenance columns are widened from VARCHAR(17) to VARCHAR(32) so `alembic check` is clean there too.
"""
from alembic import op
import sqlalchemy as sa

revision = "0015_schema_alignment"
down_revision = "0014_health_action_confirmation"
branch_labels = None
depends_on = None

# (table, column, varchar length, native enum type, enum values)
_PROV = ("USER_REPORTED", "FAMILY_REPORTED", "DOCUMENT_VERIFIED", "SYSTEM_DERIVED")
_DOC = ("UPLOADED", "QUEUED", "PROCESSING", "COMPLETED", "FAILED", "DELETED", "DELETION_PENDING")
_COLUMNS = [
    ("documents", "status", 16, "documentstatus_document", _DOC),
    ("processing_jobs", "status", 16, "documentstatus_job", _DOC),
    ("health_events", "provenance", 32, "provenance_health_event", _PROV),
    ("investigations", "provenance", 32, "provenance_investigation", _PROV),
    ("medications", "provenance", 32, "provenance_medication", _PROV),
    ("medication_events", "provenance", 32, "provenance_medication_event", _PROV),
]


def _pg() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _sqlite_widen():
    """SQLite stored these enums as VARCHAR(len(longest label)) (17 for provenance); widen to the model's VARCHAR(32)."""
    for table, col, length, _enum, _values in _COLUMNS:
        if col == "provenance":
            with op.batch_alter_table(table, recreate="always") as batch:
                batch.alter_column(col, existing_type=sa.String(17), type_=sa.String(length), existing_nullable=False)


def upgrade():
    if op.get_bind().dialect.name == "sqlite":
        _sqlite_widen()
        return
    if not _pg():
        return
    for table, col, length, enum_name, _values in _COLUMNS:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN {col} DROP DEFAULT")
        op.execute(f"ALTER TABLE {table} ALTER COLUMN {col} TYPE VARCHAR({length}) USING {col}::text")
        op.execute(f"DROP TYPE IF EXISTS {enum_name}")
    op.execute("ALTER TABLE temporary_shares DROP CONSTRAINT IF EXISTS temporary_shares_token_hash_key")


def downgrade():
    if not _pg():
        return
    op.execute("ALTER TABLE temporary_shares ADD CONSTRAINT temporary_shares_token_hash_key UNIQUE (token_hash)")
    for table, col, _length, enum_name, values in _COLUMNS:
        literals = ", ".join(f"'{v}'" for v in values)
        op.execute(f"CREATE TYPE {enum_name} AS ENUM ({literals})")
        op.execute(f"ALTER TABLE {table} ALTER COLUMN {col} TYPE {enum_name} USING {col}::{enum_name}")
