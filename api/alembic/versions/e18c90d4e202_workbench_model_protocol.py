"""Select Responses or Chat Completions for workbench construction."""

from alembic import op
import sqlalchemy as sa

revision = "e18c90d4e202"
down_revision = "d17b90c4e201"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "workbench_sessions",
        sa.Column(
            "model_protocol", sa.String(16), server_default="chat", nullable=False
        ),
    )


def downgrade():
    op.drop_column("workbench_sessions", "model_protocol")
