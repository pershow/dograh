"""Persist scoped business action claims and ambiguous outcomes."""

import sqlalchemy as sa
from alembic import op

revision = "b41e03f7c505"
down_revision = "a30e02f6b404"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "action_operations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column(
            "workflow_run_id",
            sa.Integer(),
            sa.ForeignKey("workflow_runs.id"),
            nullable=False,
        ),
        sa.Column("operation_key", sa.String(64), nullable=False),
        sa.Column("tool_uuid", sa.String(100), nullable=False),
        sa.Column("arguments", sa.JSON(), nullable=False),
        sa.Column("arguments_hash", sa.String(64), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("request_turn", sa.Integer(), nullable=False),
        sa.Column("owner_token", sa.String(36)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("result", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "organization_id",
            "workflow_run_id",
            "operation_key",
            name="uq_action_operation",
        ),
    )


def downgrade():
    op.drop_table("action_operations")
