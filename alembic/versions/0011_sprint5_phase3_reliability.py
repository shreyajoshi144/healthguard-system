"""Sprint 5 Phase 3 reliability fields for durable document processing jobs."""
from alembic import op
import sqlalchemy as sa

revision = "0011_sprint5_phase3_reliability"
down_revision = "0010_sprint5_phase1_security_hardening"
branch_labels = None
depends_on = None

def upgrade():
    op.add_column("processing_jobs", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("processing_jobs", sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("processing_jobs", sa.Column("last_error", sa.String(length=120), nullable=True))
    op.create_index("ix_processing_jobs_lease_expires_at", "processing_jobs", ["lease_expires_at"])
    op.create_index("ix_processing_jobs_next_attempt_at", "processing_jobs", ["next_attempt_at"])

def downgrade():
    op.drop_index("ix_processing_jobs_next_attempt_at", table_name="processing_jobs")
    op.drop_index("ix_processing_jobs_lease_expires_at", table_name="processing_jobs")
    op.drop_column("processing_jobs", "last_error")
    op.drop_column("processing_jobs", "next_attempt_at")
    op.drop_column("processing_jobs", "lease_expires_at")
