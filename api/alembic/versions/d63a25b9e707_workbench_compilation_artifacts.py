"""Store shared frozen test evidence once per workbench session."""

import sqlalchemy as sa
from alembic import op

revision = "d63a25b9e707"
down_revision = "c52f14a8d606"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "workbench_compilation_artifacts",
        sa.Column(
            "session_id",
            sa.String(36),
            sa.ForeignKey("workbench_sessions.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("fingerprint", sa.String(64), primary_key=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("workbench_compilation_artifacts")
