"""P0: user-recorded health measurements (vitals) with provenance."""
from alembic import op
import sqlalchemy as sa

revision = "0012_p0_measurements"
down_revision = "0011_sprint5_phase3_reliability"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table(
        "measurements",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("patient_id", sa.Uuid(), sa.ForeignKey("patient_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("value_secondary", sa.Float(), nullable=True),
        sa.Column("unit", sa.String(16), nullable=False),
        sa.Column("measured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("context", sa.String(32), nullable=True),
        sa.Column("note", sa.String(500), nullable=True),
        sa.Column("provenance", sa.String(32), nullable=False),
        sa.Column("source_type", sa.String(16), nullable=False, server_default="MANUAL"),
        sa.Column("source_document_id", sa.Uuid(), sa.ForeignKey("documents.id", ondelete="SET NULL"), nullable=True),
        sa.Column("source_fact_id", sa.Uuid(), sa.ForeignKey("health_facts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_measurements_patient_id", "measurements", ["patient_id"])
    op.create_index("ix_measurements_patient_kind_time", "measurements", ["patient_id", "kind", "measured_at"])

def downgrade():
    op.drop_index("ix_measurements_patient_kind_time", table_name="measurements")
    op.drop_index("ix_measurements_patient_id", table_name="measurements")
    op.drop_table("measurements")
