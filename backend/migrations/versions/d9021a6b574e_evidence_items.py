"""Store report evidence source metadata.

Revision ID: d9021a6b574e
Revises: c714b0f9a201
"""

from alembic import op
import sqlalchemy as sa


revision = "d9021a6b574e"
down_revision = "c714b0f9a201"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "evidence_items",
        sa.Column("id", sa.String(length=80), primary_key=True),
        sa.Column("task_id", sa.String(length=40), sa.ForeignKey("inspection_tasks.id"), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.String(length=80), nullable=False),
        sa.Column("source_version", sa.String(length=100), nullable=False),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("excerpt", sa.Text(), nullable=False),
        sa.Column("validity", sa.String(length=20), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_evidence_items_task_id", "evidence_items", ["task_id"])
    op.create_index("ix_evidence_items_tenant_id", "evidence_items", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_evidence_items_tenant_id", table_name="evidence_items")
    op.drop_index("ix_evidence_items_task_id", table_name="evidence_items")
    op.drop_table("evidence_items")
