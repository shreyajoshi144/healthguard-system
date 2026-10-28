"""Sprint 2 Phase 1 document processing evidence and READY state."""
from alembic import op
import sqlalchemy as sa

revision="0004_sprint2_document_processing"
down_revision="0003_final_sprint1_hardening"
branch_labels=None
depends_on=None

def upgrade():
    bind=op.get_bind()
    op.create_table(
        "document_text_blocks",
        sa.Column("id",sa.Uuid(as_uuid=True),primary_key=True),
        sa.Column("document_id",sa.Uuid(as_uuid=True),sa.ForeignKey("documents.id",ondelete="CASCADE"),nullable=False),
        sa.Column("page_number",sa.Integer(),nullable=False),
        sa.Column("block_index",sa.Integer(),nullable=False),
        sa.Column("source_text",sa.Text(),nullable=False),
        sa.Column("extraction_method",sa.String(32),nullable=False),
        sa.Column("confidence",sa.Float()),
        sa.Column("created_at",sa.DateTime(timezone=True),nullable=False),
        sa.UniqueConstraint("document_id","page_number","block_index",name="uq_document_text_block"),
    )
    op.create_index("ix_document_text_blocks_document_id","document_text_blocks",["document_id"])
    op.create_index("ix_document_text_blocks_document_page","document_text_blocks",["document_id","page_number"])

def downgrade():
    op.drop_index("ix_document_text_blocks_document_page",table_name="document_text_blocks")
    op.drop_index("ix_document_text_blocks_document_id",table_name="document_text_blocks")
    op.drop_table("document_text_blocks")
    # PostgreSQL enum values are intentionally retained for downgrade safety.
