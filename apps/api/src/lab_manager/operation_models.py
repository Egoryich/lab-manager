"""PostgreSQL-backed jobs; no provider effects are inferred from a job lease."""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from lab_manager.models import Base

TERMINAL = ("SUCCEEDED", "FAILED", "CANCELLED")


class Operation(Base):
    __tablename__ = "operations"
    __table_args__ = (
        UniqueConstraint("actor_id", "request_id"),
        CheckConstraint(
            "state IN ('QUEUED','RUNNING','WAITING_NODE','WAITING_RECONCILIATION',"
            "'SUCCEEDED','FAILED','CANCEL_REQUESTED','CANCELLED')",
            name="state",
        ),
        CheckConstraint("attempt >= 0 AND fence >= 0 AND version > 0", name="counters"),
        CheckConstraint("expected_version > 0", name="expected_version"),
        CheckConstraint(
            "(state = 'RUNNING') = (lease_until IS NOT NULL AND lease_owner IS NOT NULL)",
            name="running_lease",
        ),
        CheckConstraint(
            "(state IN ('SUCCEEDED','FAILED','CANCELLED')) = (finished_at IS NOT NULL)",
            name="terminal_time",
        ),
        Index(
            "uq_operation_active_environment",
            "environment_id",
            unique=True,
            postgresql_where=text("state NOT IN ('SUCCEEDED','FAILED','CANCELLED')"),
        ),
        Index("ix_operation_dispatch", "kind", "state", "available_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    actor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    owner_teacher_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    environment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("environments.id"))
    kind: Mapped[str] = mapped_column(String(64))
    request_id: Mapped[uuid.UUID]
    request_digest: Mapped[str] = mapped_column(String(64))
    expected_version: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(32), default="QUEUED")
    version: Mapped[int] = mapped_column(Integer, default=1)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    fence: Mapped[int] = mapped_column(BigInteger, default=0)
    lease_owner: Mapped[uuid.UUID | None]
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    result: Mapped[dict | None] = mapped_column(JSONB)


class OperationEvent(Base):
    """Durable outbox facts. Consumers must deduplicate by id, not by Redis delivery."""

    __tablename__ = "operation_events"
    __table_args__ = (UniqueConstraint("operation_id", "version"),)
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    operation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("operations.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
