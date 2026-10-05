"""Store per-runtime SSH keys encrypted for browser terminal grants.

Revision ID: 0012_guest_ssh
Revises: 0011_template_ref
"""

import sqlalchemy as sa
from alembic import op

revision = "0012_guest_ssh"
down_revision = "0011_template_ref"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "runtime_ssh_credentials",
        sa.Column("runtime_id", sa.Uuid(), primary_key=True),
        sa.Column("public_key", sa.String(256), nullable=False),
        sa.Column("private_key_ciphertext", sa.String(4096), nullable=False),
        sa.Column("host_key", sa.String(2048), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["runtime_id"], ["runtimes.id"]),
    )


def downgrade():
    op.drop_table("runtime_ssh_credentials")
