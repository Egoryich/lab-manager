"""Bounded profile sizing and pinned environment resource choices.

Revision ID: 0005_sizing
Revises: 0004_nodes
"""

import sqlalchemy as sa
from alembic import op

revision = "0005_sizing"
down_revision = "0004_nodes"
branch_labels = None
depends_on = None

DIMENSIONS = ("memory_mib", "vcpu", "disk_gib")


def upgrade():
    for dimension in DIMENSIONS:
        for boundary in ("min", "max"):
            op.add_column(
                "profile_versions",
                sa.Column(f"{boundary}_{dimension}", sa.Integer(), nullable=True),
            )
    # Existing immutable profiles become fixed-size profiles without changing
    # their original resource values or the environments that reference them.
    op.execute("ALTER TABLE profile_versions DISABLE TRIGGER immutable_revision")
    op.execute(
        "UPDATE profile_versions SET "
        + ", ".join(
            f"{boundary}_{dimension} = {dimension}"
            for dimension in DIMENSIONS
            for boundary in ("min", "max")
        )
    )
    op.execute("ALTER TABLE profile_versions ENABLE TRIGGER immutable_revision")
    for dimension in DIMENSIONS:
        for boundary in ("min", "max"):
            op.alter_column("profile_versions", f"{boundary}_{dimension}", nullable=False)
    op.create_check_constraint(
        op.f("ck_profile_versions_resource_range"),
        "profile_versions",
        "min_memory_mib >= 128 AND min_memory_mib <= memory_mib "
        "AND memory_mib <= max_memory_mib "
        "AND min_vcpu >= 1 AND min_vcpu <= vcpu AND vcpu <= max_vcpu "
        "AND min_disk_gib >= 1 AND min_disk_gib <= disk_gib "
        "AND disk_gib <= max_disk_gib",
    )

    for owner in ("student", "demo"):
        for dimension in DIMENSIONS:
            op.add_column(
                "environments",
                sa.Column(f"{owner}_{dimension}", sa.Integer(), nullable=True),
            )
    op.execute(
        "UPDATE environments AS e SET "
        "student_memory_mib = p.memory_mib, student_vcpu = p.vcpu, "
        "student_disk_gib = p.disk_gib, demo_memory_mib = d.memory_mib, "
        "demo_vcpu = d.vcpu, demo_disk_gib = d.disk_gib "
        "FROM profile_versions AS p, profile_versions AS d "
        "WHERE e.profile_version_id = p.id AND e.demo_profile_version_id = d.id"
    )
    for owner in ("student", "demo"):
        for dimension in DIMENSIONS:
            op.alter_column("environments", f"{owner}_{dimension}", nullable=False)
    op.create_check_constraint(
        op.f("ck_environments_selected_resources"),
        "environments",
        "student_memory_mib >= 128 AND student_vcpu >= 1 AND student_disk_gib >= 1 "
        "AND demo_memory_mib >= 128 AND demo_vcpu >= 1 AND demo_disk_gib >= 1",
    )


def downgrade():
    op.drop_constraint(op.f("ck_environments_selected_resources"), "environments")
    for owner in ("student", "demo"):
        for dimension in DIMENSIONS:
            op.drop_column("environments", f"{owner}_{dimension}")
    op.drop_constraint(op.f("ck_profile_versions_resource_range"), "profile_versions")
    for dimension in DIMENSIONS:
        for boundary in ("min", "max"):
            op.drop_column("profile_versions", f"{boundary}_{dimension}")
