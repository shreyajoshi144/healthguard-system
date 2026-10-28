"""Sprint 2 Phase 3 longitudinal temporal metadata."""
from alembic import op
import sqlalchemy as sa

revision = "0006_sprint2_phase3_longitudinal"
down_revision = "0005_sprint2_phase2_health_facts"
branch_labels = None
depends_on = None

def upgrade():
    op.add_column("health_facts", sa.Column("temporal_date", sa.Date(), nullable=True))
    op.add_column("health_facts", sa.Column("temporal_precision", sa.String(16), nullable=False, server_default="UNKNOWN"))
    op.add_column("health_facts", sa.Column("source_date_text", sa.Text(), nullable=True))
    op.create_index("ix_health_facts_patient_temporal_date", "health_facts", ["patient_id", "temporal_date"])

def downgrade():
    op.drop_index("ix_health_facts_patient_temporal_date", table_name="health_facts")
    op.drop_column("health_facts", "source_date_text")
    op.drop_column("health_facts", "temporal_precision")
    op.drop_column("health_facts", "temporal_date")
