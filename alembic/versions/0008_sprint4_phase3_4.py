from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import sqlite

revision = "0008_sprint4_phase3_4"
down_revision = "0007_sprint4_care_continuity"
branch_labels = None
depends_on = None

def upgrade():
    op.add_column("patient_access", sa.Column("status", sa.String(length=16), nullable=True))
    op.add_column("patient_access", sa.Column("scopes", sa.JSON(), nullable=True))
    op.add_column("patient_access", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("patient_access", sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE patient_access SET status='ACTIVE' WHERE status IS NULL")
    op.execute("UPDATE patient_access SET scopes='[\"PROFILE\",\"DOCUMENTS\",\"MEDICATIONS\",\"INVESTIGATIONS\",\"TIMELINE\",\"HANDOFFS\",\"HEALTH_ACTIONS\"]' WHERE scopes IS NULL")
    op.execute("UPDATE patient_access SET updated_at=created_at WHERE updated_at IS NULL")
    with op.batch_alter_table("patient_access") as b:
        b.alter_column("status", nullable=False, server_default="ACTIVE")
        b.alter_column("scopes", nullable=False, server_default=sa.text("'[]'"))
        b.alter_column("updated_at", nullable=False)
    op.create_table("temporary_shares",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("patient_id", sa.Uuid(), sa.ForeignKey("patient_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column("role", sa.String(length=16), nullable=False, server_default="DOCTOR"),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("resources", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_temporary_shares_patient_id", "temporary_shares", ["patient_id"])
    op.create_index("ix_temporary_shares_token_hash", "temporary_shares", ["token_hash"], unique=True)
    op.create_index("ix_temporary_shares_expires_at", "temporary_shares", ["expires_at"])
    op.create_index("ix_temporary_shares_patient_active", "temporary_shares", ["patient_id","expires_at","revoked_at"])

def downgrade():
    op.drop_table("temporary_shares")
    with op.batch_alter_table("patient_access") as b:
        b.drop_column("updated_at"); b.drop_column("expires_at"); b.drop_column("scopes"); b.drop_column("status")
