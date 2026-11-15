"""Add configurable flag delivery to container challenges.

Revision ID: f0a5b6c7d8e9
Revises: e9f4a5b6c7d8
"""
from alembic import op
import sqlalchemy as sa


revision = "f0a5b6c7d8e9"
down_revision = "e9f4a5b6c7d8"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if "container_challenge" not in inspector.get_table_names():
        return

    op.add_column(
        "container_challenge",
        sa.Column("flag_delivery_mode", sa.String(length=20), nullable=False, server_default="environment"),
    )
    op.add_column(
        "container_challenge",
        sa.Column("flag_file_path", sa.String(length=512), nullable=True, server_default="/home/ctf/flag.txt"),
    )
    op.add_column(
        "container_challenge",
        sa.Column("flag_file_owner", sa.String(length=64), nullable=True, server_default="target"),
    )
    op.add_column(
        "container_challenge",
        sa.Column("flag_file_group", sa.String(length=64), nullable=True, server_default="target"),
    )
    op.add_column(
        "container_challenge",
        sa.Column("flag_file_mode", sa.String(length=4), nullable=True, server_default="440"),
    )
    for column in ("flag_delivery_mode", "flag_file_path", "flag_file_owner", "flag_file_group", "flag_file_mode"):
        op.alter_column("container_challenge", column, server_default=None)


def downgrade():
    for column in ("flag_file_mode", "flag_file_group", "flag_file_owner", "flag_file_path", "flag_delivery_mode"):
        op.drop_column("container_challenge", column)
