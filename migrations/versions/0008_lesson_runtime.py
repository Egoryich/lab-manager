"""Persist lesson runs, guest identities and observed provider bindings.

Revision ID: 0008_runtime
Revises: 0007_network
"""

import sqlalchemy as sa
from alembic import op

revision = "0008_runtime"
down_revision = "0007_network"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "environment_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column("node_id", sa.Uuid(), nullable=False),
        sa.Column("reservation_id", sa.Uuid(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("stopped_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "state IN ('PLANNED','PREPARING','READY','RUNNING','STOPPING','STOPPED',"
            "'RECONCILING','ERROR')",
            name=op.f("ck_environment_runs_state"),
        ),
        sa.CheckConstraint("generation > 0", name=op.f("ck_environment_runs_generation")),
        sa.CheckConstraint(
            "(state = 'STOPPED') = (stopped_at IS NOT NULL)",
            name=op.f("ck_environment_runs_stopped_time"),
        ),
        sa.ForeignKeyConstraint(
            ["environment_id"],
            ["environments.id"],
            name=op.f("fk_environment_runs_environment_id_environments"),
        ),
        sa.ForeignKeyConstraint(
            ["node_id"],
            ["node_observations.id"],
            name=op.f("fk_environment_runs_node_id_node_observations"),
        ),
        sa.ForeignKeyConstraint(
            ["reservation_id"],
            ["lesson_reservations.id"],
            name=op.f("fk_environment_runs_reservation_id_lesson_reservations"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_environment_runs")),
        sa.UniqueConstraint(
            "environment_id", "generation", name=op.f("uq_environment_runs_environment_id")
        ),
        sa.UniqueConstraint("id", "environment_id", name="uq_environment_runs_id_environment_id"),
        sa.UniqueConstraint("reservation_id", name=op.f("uq_environment_runs_reservation_id")),
    )
    op.create_index(
        "uq_environment_runs_unfinished",
        "environment_runs",
        ["environment_id"],
        unique=True,
        postgresql_where=sa.text("state != 'STOPPED'"),
    )
    op.create_table(
        "runtimes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column("node_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(8), nullable=False),
        sa.Column("student_id", sa.Uuid()),
        sa.Column("membership_generation", sa.Integer()),
        sa.Column("profile_version_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("memory_mib", sa.Integer(), nullable=False),
        sa.Column("vcpu", sa.Integer(), nullable=False),
        sa.Column("disk_gib", sa.Integer(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("observed_at", sa.DateTime(timezone=True)),
        sa.Column("last_activity_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("role IN ('STUDENT','DEMO')", name=op.f("ck_runtimes_role")),
        sa.CheckConstraint("kind IN ('LXC','QEMU')", name=op.f("ck_runtimes_kind")),
        sa.CheckConstraint(
            "state IN ('PLANNED','PROVISIONING','STOPPED','STARTING','RUNNING',"
            "'HIBERNATING','HIBERNATED','STOPPING','RECONCILING','ERROR','DELETING','DELETED')",
            name=op.f("ck_runtimes_state"),
        ),
        sa.CheckConstraint(
            "(role = 'STUDENT' AND student_id IS NOT NULL AND membership_generation IS NOT NULL)"
            " OR (role = 'DEMO' AND student_id IS NULL AND membership_generation IS NULL)",
            name=op.f("ck_runtimes_owner_role"),
        ),
        sa.CheckConstraint(
            "memory_mib >= 128 AND vcpu >= 1 AND disk_gib >= 1", name=op.f("ck_runtimes_size")
        ),
        sa.CheckConstraint("generation > 0", name=op.f("ck_runtimes_generation")),
        sa.CheckConstraint(
            "(state = 'DELETED') = (deleted_at IS NOT NULL)",
            name=op.f("ck_runtimes_deleted_time"),
        ),
        sa.CheckConstraint(
            "membership_generation IS NULL OR membership_generation > 0",
            name=op.f("ck_runtimes_membership_generation"),
        ),
        sa.ForeignKeyConstraint(
            ["environment_id"],
            ["environments.id"],
            name=op.f("fk_runtimes_environment_id_environments"),
        ),
        sa.ForeignKeyConstraint(
            ["node_id"],
            ["node_observations.id"],
            name=op.f("fk_runtimes_node_id_node_observations"),
        ),
        sa.ForeignKeyConstraint(
            ["student_id"], ["users.id"], name=op.f("fk_runtimes_student_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["profile_version_id"],
            ["profile_versions.id"],
            name=op.f("fk_runtimes_profile_version_id_profile_versions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runtimes")),
        sa.UniqueConstraint("id", "environment_id", name="uq_runtimes_id_environment_id"),
        sa.UniqueConstraint("id", "node_id", name="uq_runtimes_id_node_id"),
    )
    op.create_index(
        "uq_runtimes_active_student",
        "runtimes",
        ["environment_id", "student_id"],
        unique=True,
        postgresql_where=sa.text("role = 'STUDENT' AND deleted_at IS NULL"),
    )
    op.create_index(
        "uq_runtimes_active_demo",
        "runtimes",
        ["environment_id"],
        unique=True,
        postgresql_where=sa.text("role = 'DEMO' AND deleted_at IS NULL"),
    )
    op.create_table(
        "run_runtimes",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("runtime_id", sa.Uuid(), nullable=False),
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.CheckConstraint(
            "state IN ('PLANNED','READY','ACTIVE','STOPPING','STOPPED','ERROR')",
            name=op.f("ck_run_runtimes_state"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "environment_id"],
            ["environment_runs.id", "environment_runs.environment_id"],
            name="fk_run_runtimes_run_environment",
        ),
        sa.ForeignKeyConstraint(
            ["runtime_id", "environment_id"],
            ["runtimes.id", "runtimes.environment_id"],
            name="fk_run_runtimes_runtime_environment",
        ),
        sa.PrimaryKeyConstraint("run_id", "runtime_id", name=op.f("pk_run_runtimes")),
    )
    op.create_table(
        "provider_runtime_bindings",
        sa.Column("runtime_id", sa.Uuid(), nullable=False),
        sa.Column("node_id", sa.Uuid(), nullable=False),
        sa.Column("vmid", sa.Integer(), nullable=False),
        sa.Column("ownership_marker", sa.String(128), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "vmid BETWEEN 100 AND 999999999", name=op.f("ck_provider_runtime_bindings_vmid")
        ),
        sa.CheckConstraint("generation > 0", name=op.f("ck_provider_runtime_bindings_generation")),
        sa.ForeignKeyConstraint(
            ["runtime_id", "node_id"],
            ["runtimes.id", "runtimes.node_id"],
            name="fk_provider_runtime_bindings_runtime_node",
        ),
        sa.PrimaryKeyConstraint("runtime_id", name=op.f("pk_provider_runtime_bindings")),
        sa.UniqueConstraint("node_id", "vmid", name=op.f("uq_provider_runtime_bindings_node_id")),
    )
    op.create_table(
        "runtime_disks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("runtime_id", sa.Uuid(), nullable=False),
        sa.Column("node_id", sa.Uuid(), nullable=False),
        sa.Column("storage_name", sa.String(64), nullable=False),
        sa.Column("provider_ref", sa.String(256)),
        sa.Column("logical_bytes", sa.BigInteger(), nullable=False),
        sa.Column("observed_physical_bytes", sa.BigInteger()),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "logical_bytes > 0 AND observed_physical_bytes >= 0", name=op.f("ck_runtime_disks_size")
        ),
        sa.CheckConstraint(
            "state IN ('PLANNED','PRESENT','UNKNOWN','DELETING','DELETED')",
            name=op.f("ck_runtime_disks_state"),
        ),
        sa.ForeignKeyConstraint(
            ["runtime_id", "node_id"],
            ["runtimes.id", "runtimes.node_id"],
            name="fk_runtime_disks_runtime_node",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runtime_disks")),
        sa.UniqueConstraint(
            "node_id", "storage_name", "provider_ref", name=op.f("uq_runtime_disks_node_id")
        ),
    )
    op.create_index("ix_runtime_disks_runtime_id", "runtime_disks", ["runtime_id"])


def downgrade():
    op.drop_index("ix_runtime_disks_runtime_id", table_name="runtime_disks")
    op.drop_table("runtime_disks")
    op.drop_table("provider_runtime_bindings")
    op.drop_table("run_runtimes")
    op.drop_index("uq_runtimes_active_demo", table_name="runtimes")
    op.drop_index("uq_runtimes_active_student", table_name="runtimes")
    op.drop_table("runtimes")
    op.drop_index("uq_environment_runs_unfinished", table_name="environment_runs")
    op.drop_table("environment_runs")
