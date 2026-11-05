"""Persist work order version for each reinspection round.

Revision ID: c714b0f9a201
Revises: bea246f5d070
"""

from alembic import op
import sqlalchemy as sa


revision = "c714b0f9a201"
down_revision = "bea246f5d070"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("reinspections", sa.Column("order_version", sa.Integer(),
                                             nullable=False, server_default="0"))


def downgrade() -> None:
    with op.batch_alter_table("reinspections") as batch:
        batch.drop_column("order_version")
