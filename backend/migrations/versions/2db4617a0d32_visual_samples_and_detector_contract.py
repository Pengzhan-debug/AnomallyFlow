"""Visual samples and versioned detector request/result snapshots."""
from alembic import op
import sqlalchemy as sa

revision = "2db4617a0d32"
down_revision = "9c78e31e5ddf"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("inspection_tasks", sa.Column("detector_config", sa.JSON(), nullable=True))
    op.add_column("inspection_tasks", sa.Column("detection_json", sa.JSON(), nullable=True))
    op.create_table("visual_samples",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("created_by", sa.String(40), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("product_code", sa.String(64), nullable=False),
        sa.Column("label", sa.String(16), nullable=False),
        sa.Column("split", sa.String(16), nullable=False),
        sa.Column("source", sa.String(200), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("image_key", sa.String(256), nullable=False),
        sa.Column("image_sha256", sa.String(64), nullable=False),
        sa.Column("image_mime", sa.String(32), nullable=False),
        sa.Column("image_width", sa.Integer(), nullable=False),
        sa.Column("image_height", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "image_sha256", name="uq_visual_sample_content"))
    op.create_index("ix_visual_samples_tenant_id", "visual_samples", ["tenant_id"])
    op.create_index("ix_visual_samples_product_code", "visual_samples", ["product_code"])


def downgrade():
    op.drop_index("ix_visual_samples_product_code", table_name="visual_samples")
    op.drop_index("ix_visual_samples_tenant_id", table_name="visual_samples")
    op.drop_table("visual_samples")
    op.drop_column("inspection_tasks", "detection_json")
    op.drop_column("inspection_tasks", "detector_config")
