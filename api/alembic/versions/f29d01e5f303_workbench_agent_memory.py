"""Source-backed human-confirmed memory shared by an Agent's sessions."""

import sqlalchemy as sa
from alembic import op

revision = "f29d01e5f303"
down_revision = "e18c90d4e202"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "workbench_memories",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column(
            "workflow_id", sa.Integer(), sa.ForeignKey("workflows.id"), nullable=False
        ),
        sa.Column(
            "session_id",
            sa.String(36),
            sa.ForeignKey("workbench_sessions.id"),
            nullable=False,
        ),
        sa.Column(
            "confirmed_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("source", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("replaces_id", sa.String(36), sa.ForeignKey("workbench_memories.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_workbench_memories_workflow_id", "workbench_memories", ["workflow_id"]
    )


def downgrade():
    op.drop_table("workbench_memories")
