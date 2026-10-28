"""Sprint 2 Phase 2 structured medical facts and evidence."""
from alembic import op
import sqlalchemy as sa

revision="0005_sprint2_phase2_health_facts"
down_revision="0004_sprint2_document_processing"
branch_labels=None
depends_on=None

def upgrade():
    op.create_table(
        "health_facts",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("patient_id", sa.Uuid(as_uuid=True), sa.ForeignKey("patient_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("document_id", sa.Uuid(as_uuid=True), sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("fact_type", sa.String(80), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("normalized_value", sa.Text()),
        sa.Column("source_page", sa.Integer(), nullable=False),
        sa.Column("source_text", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float()),
        sa.Column("extraction_method", sa.String(40), nullable=False),
        sa.Column("provenance", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_health_facts_patient_id", "health_facts", ["patient_id"])
    op.create_index("ix_health_facts_document_id", "health_facts", ["document_id"])
    op.create_index("ix_health_facts_patient_document", "health_facts", ["patient_id", "document_id"])

def downgrade():
    op.drop_index("ix_health_facts_patient_document", table_name="health_facts")
    op.drop_index("ix_health_facts_document_id", table_name="health_facts")
    op.drop_index("ix_health_facts_patient_id", table_name="health_facts")
    op.drop_table("health_facts")
