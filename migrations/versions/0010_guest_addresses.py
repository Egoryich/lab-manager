"""Keep a stable guest address within each reserved lesson segment.

Revision ID: 0010_guest_ipv4
Revises: 0009_commands
"""

import sqlalchemy as sa
from alembic import op

revision = "0010_guest_ipv4"
down_revision = "0009_commands"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("runtimes", sa.Column("network_allocation_id", sa.Uuid(), nullable=True))
    op.add_column("runtimes", sa.Column("guest_ipv4", sa.String(15), nullable=True))
    op.create_foreign_key(
        "fk_runtimes_network_allocation_id",
        "runtimes",
        "network_segment_allocations",
        ["network_allocation_id"],
        ["id"],
    )
    op.create_unique_constraint(
        "uq_runtimes_segment_ipv4", "runtimes", ["network_allocation_id", "guest_ipv4"]
    )
    op.create_check_constraint(
        "ck_runtimes_network_address_pair",
        "runtimes",
        "(network_allocation_id IS NULL) = (guest_ipv4 IS NULL)",
    )


def downgrade():
    op.drop_constraint("ck_runtimes_network_address_pair", "runtimes", type_="check")
    op.drop_constraint("uq_runtimes_segment_ipv4", "runtimes", type_="unique")
    op.drop_constraint("fk_runtimes_network_allocation_id", "runtimes", type_="foreignkey")
    op.drop_column("runtimes", "guest_ipv4")
    op.drop_column("runtimes", "network_allocation_id")
