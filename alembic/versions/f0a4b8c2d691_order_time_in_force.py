"""Persist time in force for exact order intent replay.

Revision ID: f0a4b8c2d691
Revises: 30f78d5ef935
"""

from alembic import op
import sqlalchemy as sa

revision = "f0a4b8c2d691"
down_revision = "30f78d5ef935"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column("time_in_force", sa.String(16), nullable=False, server_default="DAY"),
    )


def downgrade() -> None:
    op.drop_column("orders", "time_in_force")
