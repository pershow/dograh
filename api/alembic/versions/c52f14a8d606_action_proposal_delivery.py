"""Bind action confirmation to delivered proposal output.

Revision ID: c52f14a8d606
Revises: b41e03f7c505
"""

from alembic import op
import sqlalchemy as sa

revision = "c52f14a8d606"
down_revision = "b41e03f7c505"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "action_operations", sa.Column("proposed_turn", sa.Integer(), nullable=True)
    )


def downgrade():
    op.drop_column("action_operations", "proposed_turn")
