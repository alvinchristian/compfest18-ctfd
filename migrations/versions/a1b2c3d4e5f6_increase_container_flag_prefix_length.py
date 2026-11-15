"""Increase container challenge flag prefix length.

Revision ID: a1b2c3d4e5f6
Revises: 2b3c4d5e6f70
"""
from alembic import op
import sqlalchemy as sa


revision = "a1b2c3d4e5f6"
down_revision = "2b3c4d5e6f70"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if "container_challenge" not in inspector.get_table_names():
        # The container plugin registers this polymorphic table after core
        # migrations run on a fresh install. Its current model already has a
        # 70-character prefix, so there is nothing to migrate yet.
        return
    columns = {column["name"] for column in inspector.get_columns("container_challenge")}
    if "flag_prefix" not in columns:
        return
    op.alter_column(
        "container_challenge",
        "flag_prefix",
        existing_type=sa.String(length=50),
        type_=sa.String(length=70),
    )


def downgrade():
    inspector = sa.inspect(op.get_bind())
    if "container_challenge" not in inspector.get_table_names():
        return
    op.alter_column(
        "container_challenge",
        "flag_prefix",
        existing_type=sa.String(length=70),
        type_=sa.String(length=50),
    )
