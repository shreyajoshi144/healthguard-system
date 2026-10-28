"""Sprint 1 hardening: provenance/auth/storage state and email verification."""
from alembic import op
import sqlalchemy as sa

revision="0002_sprint1_hardening"
down_revision="0001_healthguard_sprint1"
branch_labels=None
depends_on=None

def upgrade():
    bind=op.get_bind()
    if bind.dialect.name=="postgresql":
        op.execute("ALTER TYPE documentstatus_document ADD VALUE IF NOT EXISTS 'DELETION_PENDING'")
        op.execute("ALTER TYPE documentstatus_job ADD VALUE IF NOT EXISTS 'DELETION_PENDING'")
    else:
        # The original SQLite migration used the pre-hardening enum max length.
        # Widen both status columns so DELETION_PENDING is represented cleanly.
        with op.batch_alter_table("documents") as batch:
            batch.alter_column("status", existing_type=sa.String(length=10), type_=sa.String(length=16), existing_nullable=False)
        with op.batch_alter_table("processing_jobs") as batch:
            batch.alter_column("status", existing_type=sa.String(length=10), type_=sa.String(length=16), existing_nullable=False)
    with op.batch_alter_table("patient_access") as batch:
        batch.create_check_constraint("ck_patient_access_secondary_role", "role <> 'OWNER'")
    op.create_table("email_verification_tokens",
        sa.Column("id",sa.Uuid(as_uuid=True),primary_key=True),
        sa.Column("user_id",sa.Uuid(as_uuid=True),sa.ForeignKey("users.id",ondelete="CASCADE"),nullable=False),
        sa.Column("token_hash",sa.String(64),nullable=False),
        sa.Column("expires_at",sa.DateTime(timezone=True),nullable=False),
        sa.Column("used_at",sa.DateTime(timezone=True)),
        sa.Column("created_at",sa.DateTime(timezone=True),nullable=False),
        sa.UniqueConstraint("token_hash",name="uq_email_verification_token_hash"),
    )
    op.create_index("ix_email_verification_tokens_user_id","email_verification_tokens",["user_id"])

def downgrade():
    op.drop_index("ix_email_verification_tokens_user_id",table_name="email_verification_tokens")
    op.drop_table("email_verification_tokens")
    with op.batch_alter_table("patient_access") as batch:
        batch.drop_constraint("ck_patient_access_secondary_role", type_="check")
    # PostgreSQL enum values are intentionally not removed; doing so safely would
    # require proving no historical rows use the value. The schema remains compatible.
