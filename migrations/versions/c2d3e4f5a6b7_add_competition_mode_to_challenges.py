"""Add competition mode to challenges.

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
"""

import sqlalchemy as sa
from alembic import op


revision = "c2d3e4f5a6b7"
down_revision = "b1c2d3e4f5a6"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "challenges",
        sa.Column(
            "competition_mode",
            sa.String(length=20),
            nullable=False,
            server_default="jeopardy",
        ),
    )
    op.create_index(
        "ix_challenges_competition_mode",
        "challenges",
        ["competition_mode"],
    )

    # Preserve challenges created by the existing 1v1 seeder and challenges
    # that have already appeared in a bracket game.
    inspector = sa.inspect(op.get_bind())
    table_names = set(inspector.get_table_names())
    if "tags" in table_names:
        op.execute(
            sa.text(
                "UPDATE challenges SET competition_mode = 'onevone' "
                "WHERE id IN ("
                "SELECT challenge_id FROM tags WHERE LOWER(value) = '1v1'"
                ")"
            )
        )
    if "onevone_bracket_games" in table_names:
        op.execute(
            sa.text(
                "UPDATE challenges SET competition_mode = 'onevone' "
                "WHERE id IN ("
                "SELECT challenge_id FROM onevone_bracket_games "
                "WHERE challenge_id IS NOT NULL"
                ")"
            )
        )


def downgrade():
    op.drop_index("ix_challenges_competition_mode", table_name="challenges")
    with op.batch_alter_table("challenges") as batch_op:
        batch_op.drop_column("competition_mode")
