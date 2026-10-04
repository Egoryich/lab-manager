"""Durable, single-attempt node commands linked to a lesson operation."""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from lab_manager.models import Base


class NodeCommand(Base):
    __tablename__ = "node_commands"
    __table_args__ = (
        ForeignKeyConstraint(
            ["runtime_id", "node_id"],
            ["runtimes.id", "runtimes.node_id"],
            name="fk_node_commands_runtime_node",
        ),
        CheckConstraint("kind IN ('LXC_CREATE','LXC_START','LXC_SHUTDOWN')", name="kind"),
        CheckConstraint(
            "state IN ('QUEUED','ATTEMPTED','SUBMITTED','SUCCEEDED','FAILED','UNCERTAIN')",
            name="state",
        ),
        CheckConstraint("vmid BETWEEN 100 AND 999999999 AND generation > 0", name="identity"),
        CheckConstraint("fence >= 0", name="fence"),
        CheckConstraint("(lease_owner IS NULL) = (lease_until IS NULL)", name="lease_pair"),
        CheckConstraint(
            "(state = 'QUEUED') = (submit_attempted_at IS NULL)",
            name="attempt_recorded",
        ),
        Index("ix_node_commands_due", "state", "available_at"),
        Index(
            "uq_node_commands_active_runtime",
            "runtime_id",
            unique=True,
            postgresql_where=text("state IN ('QUEUED','ATTEMPTED','SUBMITTED','UNCERTAIN')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    parent_operation_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("operations.id"))
    runtime_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    node_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    kind: Mapped[str] = mapped_column(String(16))
    vmid: Mapped[int] = mapped_column(Integer)
    generation: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(String(16), default="QUEUED")
    fence: Mapped[int] = mapped_column(Integer, default=0)
    lease_owner: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    submit_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    receipt: Mapped[dict | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
