"""Persistent disk commitments and time-bounded compute reservations."""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from lab_manager.models import Base


class NodeResourcePolicy(Base):
    __tablename__ = "node_resource_policies"
    __table_args__ = (
        CheckConstraint(
            "host_reserve_mib >= 0 AND infrastructure_reserve_mib >= 0 "
            "AND safety_reserve_mib >= 0 AND cpu_millicredits_per_logical_cpu BETWEEN 1 AND 4000",
            name="compute_bounds",
        ),
        CheckConstraint("storage_free_percent BETWEEN 10 AND 50", name="storage_floor"),
        CheckConstraint("thin_metadata_limit_percent BETWEEN 1 AND 99", name="metadata_limit"),
    )
    node_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("node_observations.id"), primary_key=True)
    storage_name: Mapped[str] = mapped_column(String(64))
    host_reserve_mib: Mapped[int] = mapped_column(Integer)
    infrastructure_reserve_mib: Mapped[int] = mapped_column(Integer)
    safety_reserve_mib: Mapped[int] = mapped_column(Integer)
    cpu_millicredits_per_logical_cpu: Mapped[int] = mapped_column(Integer)
    storage_free_percent: Mapped[int] = mapped_column(Integer)
    thin_metadata_limit_percent: Mapped[int] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(Integer, default=1)


class NodeResourceLedger(Base):
    """One row per node; SELECT FOR UPDATE serializes all admissions on that node."""

    __tablename__ = "node_resource_ledgers"
    node_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("node_resource_policies.node_id"), primary_key=True
    )


class EnvironmentDiskAllocation(Base):
    __tablename__ = "environment_disk_allocations"
    __table_args__ = (
        CheckConstraint("disk_bytes > 0 AND hibernation_bytes >= 0", name="size"),
        CheckConstraint("state IN ('RESERVED','MATERIALIZED')", name="state"),
        Index("ix_environment_disk_allocations_node_id", "node_id"),
    )
    environment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("environments.id"), primary_key=True
    )
    node_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("node_resource_ledgers.node_id"))
    storage_name: Mapped[str] = mapped_column(String(64))
    disk_bytes: Mapped[int] = mapped_column(BigInteger)
    hibernation_bytes: Mapped[int] = mapped_column(BigInteger)
    state: Mapped[str] = mapped_column(String(16), default="RESERVED")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LessonReservation(Base):
    __tablename__ = "lesson_reservations"
    __table_args__ = (
        UniqueConstraint("teacher_id", "request_id"),
        CheckConstraint("starts_at < ends_at", name="time_window"),
        CheckConstraint("memory_mib > 0 AND cpu_millicredits > 0", name="compute_size"),
        CheckConstraint("disk_bytes > 0 AND hibernation_bytes >= 0", name="disk_size"),
        CheckConstraint("state IN ('RESERVED','ACTIVE','COMPLETED','CANCELLED')", name="state"),
        Index("ix_lesson_reservations_node_time", "node_id", "starts_at", "ends_at"),
        Index("ix_lesson_reservations_environment_id", "environment_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    node_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("node_resource_ledgers.node_id"))
    environment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("environments.id"))
    teacher_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    request_id: Mapped[uuid.UUID]
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    memory_mib: Mapped[int] = mapped_column(Integer)
    cpu_millicredits: Mapped[int] = mapped_column(Integer)
    disk_bytes: Mapped[int] = mapped_column(BigInteger)
    hibernation_bytes: Mapped[int] = mapped_column(BigInteger)
    state: Mapped[str] = mapped_column(String(16), default="RESERVED")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
