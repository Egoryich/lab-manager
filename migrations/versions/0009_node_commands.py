"""Persist exact node command payload and submission attempt.

Revision ID: 0009_commands
Revises: 0008_runtime
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009_commands"
down_revision = "0008_runtime"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "node_commands",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("parent_operation_id", sa.Uuid(), nullable=False),
        sa.Column("runtime_id", sa.Uuid(), nullable=False),
        sa.Column("node_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("vmid", sa.Integer(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("fence", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.Uuid()),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column(
            "available_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("submit_attempted_at", sa.DateTime(timezone=True)),
        sa.Column("receipt", postgresql.JSONB()),
        sa.Column("error_code", sa.String(64)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["parent_operation_id"], ["operations.id"]),
        sa.ForeignKeyConstraint(
            ["runtime_id", "node_id"],
            ["runtimes.id", "runtimes.node_id"],
            name="fk_node_commands_runtime_node",
        ),
        sa.CheckConstraint(
            "kind IN ('LXC_CREATE','LXC_START','LXC_SHUTDOWN')", name="ck_node_commands_kind"
        ),
        sa.CheckConstraint(
            "state IN ('QUEUED','ATTEMPTED','SUBMITTED','SUCCEEDED','FAILED','UNCERTAIN')",
            name="ck_node_commands_state",
        ),
        sa.CheckConstraint(
            "vmid BETWEEN 100 AND 999999999 AND generation > 0", name="ck_node_commands_identity"
        ),
        sa.CheckConstraint("fence >= 0", name="ck_node_commands_fence"),
        sa.CheckConstraint(
            "(lease_owner IS NULL) = (lease_until IS NULL)", name="ck_node_commands_lease_pair"
        ),
        sa.CheckConstraint(
            "(state = 'QUEUED') = (submit_attempted_at IS NULL)",
            name="ck_node_commands_attempt_recorded",
        ),
    )
    op.create_index("ix_node_commands_due", "node_commands", ["state", "available_at"])
    op.create_index(
        "uq_node_commands_active_runtime",
        "node_commands",
        ["runtime_id"],
        unique=True,
        postgresql_where=sa.text("state IN ('QUEUED','ATTEMPTED','SUBMITTED','UNCERTAIN')"),
    )


def downgrade():
    op.drop_index("uq_node_commands_active_runtime", table_name="node_commands")
    op.drop_index("ix_node_commands_due", table_name="node_commands")
    op.drop_table("node_commands")
