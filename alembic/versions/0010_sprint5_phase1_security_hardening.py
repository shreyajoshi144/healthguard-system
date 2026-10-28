"""Sprint 5 Phase 1 security hardening

Revision ID: 0010_sprint5_phase1_security_hardening
Revises: 0009_sprint4_scope_write_permissions
"""
from alembic import op
import sqlalchemy as sa

revision = "0010_sprint5_phase1_security_hardening"
down_revision = "0009_sprint4_scope_write_permissions"
branch_labels = None
depends_on = None

def upgrade():
    # Keep the server default permanently for compatibility with SQLite and
    # existing rows. New application writes also set the ORM default.
    op.add_column("users", sa.Column("session_version", sa.Integer(), nullable=False, server_default="1"))

def downgrade():
    op.drop_column("users", "session_version")
