"""Persist one-time Guacamole launches without browser URL credentials.

Revision ID: 0013_access_grants
Revises: 0012_guest_ssh
"""

import sqlalchemy as sa
from alembic import op

revision = "0013_access_grants"
down_revision = "0012_guest_ssh"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "access_grants",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("nonce_digest", sa.String(64), nullable=False, unique=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "browser_session_id", sa.Uuid(), sa.ForeignKey("browser_sessions.id"), nullable=False
        ),
        sa.Column("runtime_id", sa.Uuid(), sa.ForeignKey("runtimes.id"), nullable=False),
        sa.Column(
            "environment_run_id", sa.Uuid(), sa.ForeignKey("environment_runs.id"), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_access_grants_browser_runtime",
        "access_grants",
        ["browser_session_id", "runtime_id"],
    )


def downgrade():
    op.drop_index("ix_access_grants_browser_runtime", table_name="access_grants")
    op.drop_table("access_grants")
