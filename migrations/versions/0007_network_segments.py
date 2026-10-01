"""Record serialized guest address pools and durable segment allocations.

Revision ID: 0007_network
Revises: 0006_ledger
"""

import sqlalchemy as sa
from alembic import op

revision = "0007_network"
down_revision = "0006_ledger"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "node_network_pools",
        sa.Column("node_id", sa.Uuid(), nullable=False),
        sa.Column("cidr", sa.String(18), nullable=False),
        sa.ForeignKeyConstraint(
            ["node_id"],
            ["node_observations.id"],
            name=op.f("fk_node_network_pools_node_id_node_observations"),
        ),
        sa.PrimaryKeyConstraint("node_id", name=op.f("pk_node_network_pools")),
    )
    op.create_table(
        "network_segment_allocations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("node_id", sa.Uuid(), nullable=False),
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column("segment_key", sa.Uuid(), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("requested_hosts", sa.Integer(), nullable=False),
        sa.Column("cidr", sa.String(18), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "mode IN ('ISOLATED','GROUP_LAN')",
            name=op.f("ck_network_segment_allocations_mode"),
        ),
        sa.CheckConstraint(
            "requested_hosts BETWEEN 2 AND 65534",
            name=op.f("ck_network_segment_allocations_hosts"),
        ),
        sa.CheckConstraint(
            "state IN ('RESERVED','APPLIED','RELEASING','RELEASED')",
            name=op.f("ck_network_segment_allocations_state"),
        ),
        sa.ForeignKeyConstraint(
            ["node_id"],
            ["node_network_pools.node_id"],
            name=op.f("fk_network_segment_allocations_node_id_node_network_pools"),
        ),
        sa.ForeignKeyConstraint(
            ["environment_id"],
            ["environments.id"],
            name=op.f("fk_network_segment_allocations_environment_id_environments"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_network_segment_allocations")),
        sa.UniqueConstraint(
            "environment_id",
            "segment_key",
            name=op.f("uq_network_segment_allocations_environment_id"),
        ),
    )
    op.create_index(
        "ix_network_segment_allocations_node_state",
        "network_segment_allocations",
        ["node_id", "state"],
    )
    op.create_index(
        "uq_network_segment_allocations_active_cidr",
        "network_segment_allocations",
        ["node_id", "cidr"],
        unique=True,
        postgresql_where=sa.text("state != 'RELEASED'"),
    )


def downgrade():
    op.drop_index(
        "uq_network_segment_allocations_active_cidr", table_name="network_segment_allocations"
    )
    op.drop_index(
        "ix_network_segment_allocations_node_state", table_name="network_segment_allocations"
    )
    op.drop_table("network_segment_allocations")
    op.drop_table("node_network_pools")
