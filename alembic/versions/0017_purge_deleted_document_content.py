"""Scrub extracted text and facts left behind by documents that were deleted before purge-on-delete existed.

Data-only migration. Only rows of documents whose status is already DELETED are removed (their stored file is gone and
the application hides them everywhere); documents that are live or DELETION_PENDING are untouched. Not reversible:
the downgrade is a deliberate no-op because purged personal data must not be resurrected.
"""
from alembic import op

revision = "0017_purge_deleted_document_content"
down_revision = "0016_email_reminders"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("DELETE FROM document_text_blocks WHERE document_id IN (SELECT id FROM documents WHERE status = 'DELETED')")
    op.execute("DELETE FROM health_facts WHERE document_id IN (SELECT id FROM documents WHERE status = 'DELETED')")


def downgrade():
    pass
