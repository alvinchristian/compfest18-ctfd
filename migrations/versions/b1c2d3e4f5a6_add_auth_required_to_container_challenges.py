"""Add per-challenge authentication requirement.

Revision ID: b1c2d3e4f5a6
Revises: a1b2c3d4e5f6
"""
from alembic import op
import sqlalchemy as sa


revision = "b1c2d3e4f5a6"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if "container_challenge" not in inspector.get_table_names():
        # See the preceding migration: this table is plugin-owned and may not
        # exist until the plugin is loaded after core migrations complete.
        return
    columns = {column["name"] for column in inspector.get_columns("container_challenge")}
    if "auth_required" in columns:
        return
    op.add_column(
        "container_challenge",
        sa.Column("auth_required", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.alter_column("container_challenge", "auth_required", server_default=None)


def downgrade():
    inspector = sa.inspect(op.get_bind())
    if "container_challenge" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("container_challenge")}
    if "auth_required" not in columns:
        return
    op.drop_column("container_challenge", "auth_required")
