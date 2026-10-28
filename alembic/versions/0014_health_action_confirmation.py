"""Persist user confirmation of document-derived follow-up suggestions."""
from alembic import op
import sqlalchemy as sa

revision = "0014_health_action_confirmation"
down_revision = "0013_p0_admin_ops"
branch_labels = None
depends_on = None

def upgrade():
    op.add_column("health_actions", sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True))
    # Legacy rows: a document-derived action already moved past PLANNED (SCHEDULED/COMPLETED/CANCELLED) was acted on by the user.
    op.execute("UPDATE health_actions SET confirmed_at = updated_at WHERE source = 'DOCUMENT_DERIVED' AND status IN ('SCHEDULED','COMPLETED')")

def downgrade():
    op.drop_column("health_actions", "confirmed_at")
