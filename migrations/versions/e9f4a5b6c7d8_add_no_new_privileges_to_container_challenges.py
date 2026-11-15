"""Add per-challenge no-new-privileges setting.

Revision ID: e9f4a5b6c7d8
Revises: d8e3f4a5b6c7
"""
from alembic import op
import sqlalchemy as sa


revision = "e9f4a5b6c7d8"
down_revision = "d8e3f4a5b6c7"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if "container_challenge" not in inspector.get_table_names():
        return

    op.add_column(
        "container_challenge",
        sa.Column(
            "no_new_privileges",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    op.alter_column("container_challenge", "no_new_privileges", server_default=None)


def downgrade():
    op.drop_column("container_challenge", "no_new_privileges")
