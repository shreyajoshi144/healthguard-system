"""P0: admin grants, ops alerts, security events, job quarantine."""
from alembic import op
import sqlalchemy as sa

revision = "0013_p0_admin_ops"
down_revision = "0012_p0_measurements"
branch_labels = None
depends_on = None

def upgrade():
    op.add_column("processing_jobs", sa.Column("quarantined_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("processing_jobs", sa.Column("quarantine_reason", sa.String(60), nullable=True))
    op.create_index("ix_processing_jobs_quarantined_at", "processing_jobs", ["quarantined_at"])
    op.create_table(
        "admin_grants",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("totp_salt", sa.String(64), nullable=False),
        sa.Column("last_totp_step", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "ops_alerts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("fingerprint", sa.String(80), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("detail", sa.String(300), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("occurrences", sa.Integer(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acknowledged_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivery_state", sa.String(24), nullable=False),
        sa.Column("delivery_attempts", sa.Integer(), nullable=False),
        sa.Column("last_delivery_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_ops_alerts_fingerprint", "ops_alerts", ["fingerprint"])
    op.create_index("ix_ops_alerts_status_seen", "ops_alerts", ["status", "last_seen_at"])
    op.create_table(
        "security_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("route", sa.String(120), nullable=False),
        sa.Column("method", sa.String(8), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("client_hash", sa.String(16), nullable=True),
        sa.Column("request_id", sa.String(64), nullable=True),
    )
    op.create_index("ix_security_events_occurred_at", "security_events", ["occurred_at"])
    op.create_index("ix_security_events_event_type", "security_events", ["event_type"])

def downgrade():
    op.drop_index("ix_security_events_event_type", table_name="security_events")
    op.drop_index("ix_security_events_occurred_at", table_name="security_events")
    op.drop_table("security_events")
    op.drop_index("ix_ops_alerts_status_seen", table_name="ops_alerts")
    op.drop_index("ix_ops_alerts_fingerprint", table_name="ops_alerts")
    op.drop_table("ops_alerts")
    op.drop_table("admin_grants")
    op.drop_index("ix_processing_jobs_quarantined_at", table_name="processing_jobs")
    op.drop_column("processing_jobs", "quarantine_reason")
    op.drop_column("processing_jobs", "quarantined_at")
