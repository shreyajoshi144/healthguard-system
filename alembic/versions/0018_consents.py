"""Explicit, versioned privacy consents (AI processing).

Revision ID: 0018_consents
"""
import sqlalchemy as sa
from alembic import op

revision = "0018_consents"
down_revision = "0017_purge_deleted_document_content"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "consents",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("purpose", sa.String(40), nullable=False),
        sa.Column("policy_version", sa.String(20), nullable=False),
        sa.Column("granted", sa.Boolean(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "purpose", name="uq_consents_user_purpose"),
    )
    op.create_index("ix_consents_user_id", "consents", ["user_id"])


def downgrade():
    op.drop_index("ix_consents_user_id", table_name="consents")
    op.drop_table("consents")
