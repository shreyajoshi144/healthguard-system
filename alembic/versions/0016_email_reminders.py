"""Email reminders for confirmed/scheduled follow-ups: preferences + delivery ledger."""
from alembic import op
import sqlalchemy as sa

revision = "0016_email_reminders"
down_revision = "0015_schema_alignment"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table(
        "reminder_preferences",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("patient_id", sa.Uuid(), sa.ForeignKey("patient_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("lead_days", sa.JSON(), nullable=False),
        sa.Column("send_hour", sa.Integer(), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("include_title", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "patient_id", name="uq_reminder_pref_user_patient"),
    )
    op.create_index("ix_reminder_preferences_user_id", "reminder_preferences", ["user_id"])
    op.create_index("ix_reminder_preferences_patient_id", "reminder_preferences", ["patient_id"])
    op.create_table(
        "reminder_deliveries",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("patient_id", sa.Uuid(), sa.ForeignKey("patient_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("action_id", sa.Uuid(), sa.ForeignKey("health_actions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("lead_days", sa.Integer(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(40), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "action_id", "lead_days", "due_date", name="uq_reminder_delivery_idempotency"),
    )
    for col in ("user_id", "patient_id", "action_id"):
        op.create_index(f"ix_reminder_deliveries_{col}", "reminder_deliveries", [col])
    op.create_index("ix_reminder_deliveries_due", "reminder_deliveries", ["status", "scheduled_for"])

def downgrade():
    op.drop_table("reminder_deliveries")
    op.drop_table("reminder_preferences")
