"""Final Sprint 1 hardening: durable account/patient deletion state."""
from alembic import op
import sqlalchemy as sa

revision="0003_final_sprint1_hardening"
down_revision="0002_sprint1_hardening"
branch_labels=None
depends_on=None

def upgrade():
    bind=op.get_bind()
    op.add_column("users", sa.Column("deletion_pending", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("patient_profiles", sa.Column("deletion_pending", sa.Boolean(), nullable=False, server_default=sa.false()))
    # SQLite cannot ALTER COLUMN to remove a server default without rebuilding
    # the table. The constant FALSE default is harmless and keeps the migration
    # portable; PostgreSQL can drop it after existing rows are backfilled.
    if bind.dialect.name != "sqlite":
        op.alter_column("users", "deletion_pending", server_default=None)
        op.alter_column("patient_profiles", "deletion_pending", server_default=None)

def downgrade():
    op.drop_column("patient_profiles", "deletion_pending")
    op.drop_column("users", "deletion_pending")
