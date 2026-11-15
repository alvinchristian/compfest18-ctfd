"""Add AI links and solver files to solves.

Revision ID: 2b3c4d5e6f70
Revises: 1a2b3c4d5e6f
"""

import sqlalchemy as sa
from alembic import op


revision = "2b3c4d5e6f70"
down_revision = "1a2b3c4d5e6f"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("solves") as batch_op:
        batch_op.add_column(sa.Column("ai_links", sa.JSON(), nullable=True))
    with op.batch_alter_table("files") as batch_op:
        batch_op.add_column(sa.Column("solve_id", sa.Integer(), nullable=True))
        batch_op.create_unique_constraint("uq_files_solve_id", ["solve_id"])
        batch_op.create_foreign_key(
            "fk_files_solve_id_solves",
            "solves",
            ["solve_id"],
            ["id"],
            ondelete="CASCADE",
        )


def downgrade():
    with op.batch_alter_table("files") as batch_op:
        if op.get_bind().dialect.name != "sqlite":
            batch_op.drop_constraint("fk_files_solve_id_solves", type_="foreignkey")
            batch_op.drop_constraint("uq_files_solve_id", type_="unique")
        batch_op.drop_column("solve_id")
    with op.batch_alter_table("solves") as batch_op:
        batch_op.drop_column("ai_links")
