"""Add platform anti-cheat alerts.

Revision ID: 1a2b3c4d5e6f
Revises: f0a5b6c7d8e9
"""

from alembic import op
import sqlalchemy as sa


revision = "1a2b3c4d5e6f"
down_revision = "f0a5b6c7d8e9"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if "anticheat_alerts" in inspector.get_table_names():
        return

    op.create_table(
        "anticheat_alerts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("alert_type", sa.String(length=64), nullable=False),
        sa.Column("fingerprint", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("ip", sa.String(length=46), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("notified_at", sa.DateTime(), nullable=True),
        sa.Column("notification_error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("fingerprint"),
    )
    op.create_index("ix_anticheat_alerts_alert_type", "anticheat_alerts", ["alert_type"])
    op.create_index("ix_anticheat_alerts_status", "anticheat_alerts", ["status"])


def downgrade():
    op.drop_index("ix_anticheat_alerts_status", table_name="anticheat_alerts")
    op.drop_index("ix_anticheat_alerts_alert_type", table_name="anticheat_alerts")
    op.drop_table("anticheat_alerts")
