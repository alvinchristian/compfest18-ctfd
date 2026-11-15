"""Add per-challenge Linux capabilities for container challenges

Revision ID: c7f1a2b3d4e5
Revises: a03403986a32
"""
import sqlalchemy as sa
from alembic import op


revision = "c7f1a2b3d4e5"
down_revision = "67ebab6de598"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "challenges",
        sa.Column("capabilities", sa.JSON(), nullable=True),
    )


def downgrade():
    op.drop_column("challenges", "capabilities")
