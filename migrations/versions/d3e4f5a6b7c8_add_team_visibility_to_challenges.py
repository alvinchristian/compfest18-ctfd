"""Add team visibility to challenges.

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
"""

from alembic import op
import sqlalchemy as sa


revision = "d3e4f5a6b7c8"
down_revision = "c2d3e4f5a6b7"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("challenges")}
    if "team_visibility" not in columns:
        op.add_column(
            "challenges",
            sa.Column("team_visibility", sa.Text(), nullable=True),
        )


def downgrade():
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("challenges")}
    if "team_visibility" in columns:
        op.drop_column("challenges", "team_visibility")
