"""Create node policies, serialized ledgers, and durable resource commitments.

Revision ID: 0006_ledger
Revises: 0005_sizing
"""

import sqlalchemy as sa
from alembic import op

revision = "0006_ledger"
down_revision = "0005_sizing"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "node_resource_policies",
        sa.Column("node_id", sa.Uuid(), nullable=False),
        sa.Column("storage_name", sa.String(64), nullable=False),
        sa.Column("host_reserve_mib", sa.Integer(), nullable=False),
        sa.Column("infrastructure_reserve_mib", sa.Integer(), nullable=False),
        sa.Column("safety_reserve_mib", sa.Integer(), nullable=False),
        sa.Column("cpu_millicredits_per_logical_cpu", sa.Integer(), nullable=False),
        sa.Column("storage_free_percent", sa.Integer(), nullable=False),
        sa.Column("thin_metadata_limit_percent", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "host_reserve_mib >= 0 AND infrastructure_reserve_mib >= 0 "
            "AND safety_reserve_mib >= 0 AND cpu_millicredits_per_logical_cpu BETWEEN 1 AND 4000",
            name=op.f("ck_node_resource_policies_compute_bounds"),
        ),
        sa.CheckConstraint(
            "storage_free_percent BETWEEN 10 AND 50",
            name=op.f("ck_node_resource_policies_storage_floor"),
        ),
        sa.CheckConstraint(
            "thin_metadata_limit_percent BETWEEN 1 AND 99",
            name=op.f("ck_node_resource_policies_metadata_limit"),
        ),
        sa.ForeignKeyConstraint(
            ["node_id"],
            ["node_observations.id"],
            name=op.f("fk_node_resource_policies_node_id_node_observations"),
        ),
        sa.PrimaryKeyConstraint("node_id", name=op.f("pk_node_resource_policies")),
    )
    op.create_table(
        "node_resource_ledgers",
        sa.Column("node_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["node_id"],
            ["node_resource_policies.node_id"],
            name=op.f("fk_node_resource_ledgers_node_id_node_resource_policies"),
        ),
        sa.PrimaryKeyConstraint("node_id", name=op.f("pk_node_resource_ledgers")),
    )
    op.create_table(
        "environment_disk_allocations",
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column("node_id", sa.Uuid(), nullable=False),
        sa.Column("storage_name", sa.String(64), nullable=False),
        sa.Column("disk_bytes", sa.BigInteger(), nullable=False),
        sa.Column("hibernation_bytes", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "disk_bytes > 0 AND hibernation_bytes >= 0",
            name=op.f("ck_environment_disk_allocations_size"),
        ),
        sa.CheckConstraint(
            "state IN ('RESERVED','MATERIALIZED')",
            name=op.f("ck_environment_disk_allocations_state"),
        ),
        sa.ForeignKeyConstraint(
            ["environment_id"],
            ["environments.id"],
            name=op.f("fk_environment_disk_allocations_environment_id_environments"),
        ),
        sa.ForeignKeyConstraint(
            ["node_id"],
            ["node_resource_ledgers.node_id"],
            name=op.f("fk_environment_disk_allocations_node_id_node_resource_ledgers"),
        ),
        sa.PrimaryKeyConstraint("environment_id", name=op.f("pk_environment_disk_allocations")),
    )
    op.create_index(
        "ix_environment_disk_allocations_node_id", "environment_disk_allocations", ["node_id"]
    )
    op.create_table(
        "lesson_reservations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("node_id", sa.Uuid(), nullable=False),
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column("teacher_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("memory_mib", sa.Integer(), nullable=False),
        sa.Column("cpu_millicredits", sa.Integer(), nullable=False),
        sa.Column("disk_bytes", sa.BigInteger(), nullable=False),
        sa.Column("hibernation_bytes", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("starts_at < ends_at", name=op.f("ck_lesson_reservations_time_window")),
        sa.CheckConstraint(
            "memory_mib > 0 AND cpu_millicredits > 0",
            name=op.f("ck_lesson_reservations_compute_size"),
        ),
        sa.CheckConstraint(
            "disk_bytes > 0 AND hibernation_bytes >= 0",
            name=op.f("ck_lesson_reservations_disk_size"),
        ),
        sa.CheckConstraint(
            "state IN ('RESERVED','ACTIVE','COMPLETED','CANCELLED')",
            name=op.f("ck_lesson_reservations_state"),
        ),
        sa.ForeignKeyConstraint(
            ["node_id"],
            ["node_resource_ledgers.node_id"],
            name=op.f("fk_lesson_reservations_node_id_node_resource_ledgers"),
        ),
        sa.ForeignKeyConstraint(
            ["environment_id"],
            ["environments.id"],
            name=op.f("fk_lesson_reservations_environment_id_environments"),
        ),
        sa.ForeignKeyConstraint(
            ["teacher_id"], ["users.id"], name=op.f("fk_lesson_reservations_teacher_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_lesson_reservations")),
        sa.UniqueConstraint(
            "teacher_id", "request_id", name=op.f("uq_lesson_reservations_teacher_id")
        ),
    )
    op.create_index(
        "ix_lesson_reservations_node_time",
        "lesson_reservations",
        ["node_id", "starts_at", "ends_at"],
    )
    op.create_index(
        "ix_lesson_reservations_environment_id", "lesson_reservations", ["environment_id"]
    )


def downgrade():
    op.drop_index("ix_lesson_reservations_environment_id", table_name="lesson_reservations")
    op.drop_index("ix_lesson_reservations_node_time", table_name="lesson_reservations")
    op.drop_table("lesson_reservations")
    op.drop_index(
        "ix_environment_disk_allocations_node_id", table_name="environment_disk_allocations"
    )
    op.drop_table("environment_disk_allocations")
    op.drop_table("node_resource_ledgers")
    op.drop_table("node_resource_policies")
