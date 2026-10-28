"""Sprint 4 Phase 1/2 care continuity artifacts."""
from alembic import op
import sqlalchemy as sa

revision = "0007_sprint4_care_continuity"
down_revision = "0006_sprint2_phase3_longitudinal"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table(
        "handoffs",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("patient_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("handoff_type", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("evidence_refs", sa.JSON(), nullable=False),
        sa.Column("generation_metadata", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["patient_id"], ["patient_profiles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_handoffs_patient_id", "handoffs", ["patient_id"])
    op.create_index("ix_handoffs_patient_created", "handoffs", ["patient_id", "created_at"])
    op.create_table(
        "health_actions",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("patient_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("action_type", sa.String(length=80), nullable=False),
        sa.Column("source", sa.String(length=24), nullable=False),
        sa.Column("provenance", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("temporal_precision", sa.String(length=16), nullable=False),
        sa.Column("source_document_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("source_fact_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("source_event_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("source_investigation_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("source_medication_event_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["patient_id"], ["patient_profiles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_document_id"], ["documents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_fact_id"], ["health_facts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_event_id"], ["health_events.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_investigation_id"], ["investigations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_medication_event_id"], ["medication_events.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_health_actions_patient_id", "health_actions", ["patient_id"])
    op.create_index("ix_health_actions_patient_status_due", "health_actions", ["patient_id", "status", "due_date"])

def downgrade():
    op.drop_index("ix_health_actions_patient_status_due", table_name="health_actions")
    op.drop_index("ix_health_actions_patient_id", table_name="health_actions")
    op.drop_table("health_actions")
    op.drop_index("ix_handoffs_patient_created", table_name="handoffs")
    op.drop_index("ix_handoffs_patient_id", table_name="handoffs")
    op.drop_table("handoffs")
