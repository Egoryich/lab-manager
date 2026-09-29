"""Persist last authenticated node inventory independently of process uptime."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004_nodes"
down_revision = "0003_operations"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "node_observations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("attempt_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_contact_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sample_finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_node_observations")),
    )


def downgrade():
    op.drop_table("node_observations")
