from alembic import op
import sqlalchemy as sa

revision = "0009_sprint4_scope_write_permissions"
down_revision = "0008_sprint4_phase3_4"
branch_labels = None
depends_on = None

def upgrade():
    op.add_column("patient_access", sa.Column("write_scopes", sa.JSON(), nullable=True))
    op.execute("UPDATE patient_access SET write_scopes='[]' WHERE write_scopes IS NULL")
    with op.batch_alter_table("patient_access") as b:
        b.alter_column("write_scopes", nullable=False, server_default=sa.text("'[]'"))

def downgrade():
    with op.batch_alter_table("patient_access") as b:
        b.drop_column("write_scopes")
