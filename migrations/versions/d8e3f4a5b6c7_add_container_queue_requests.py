"""Add persistent container queue requests"""
from alembic import op
import sqlalchemy as sa

revision = "d8e3f4a5b6c7"
down_revision = "c7f1a2b3d4e5"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table(
        "container_queue_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("uuid", sa.String(36), nullable=False, unique=True),
        sa.Column("challenge_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_container_queue_requests_status", "container_queue_requests", ["status"])
    op.create_index("ix_container_queue_requests_created_at", "container_queue_requests", ["created_at"])

def downgrade():
    op.drop_table("container_queue_requests")
