"""Bind catalog template revisions to exact Proxmox image references.

Revision ID: 0011_template_ref
Revises: 0010_guest_ipv4
"""

import sqlalchemy as sa
from alembic import op

revision = "0011_template_ref"
down_revision = "0010_guest_ipv4"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("template_versions", sa.Column("source_ref", sa.String(160), nullable=True))


def downgrade():
    op.drop_column("template_versions", "source_ref")
