"""Durable address-pool and segment claims for one Proxmox node."""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from lab_manager.models import Base


class NodeNetworkPool(Base):
    """One configured IPv4 guest pool per node; lock this row for every allocation."""

    __tablename__ = "node_network_pools"

    node_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("node_observations.id"), primary_key=True
    )
    cidr: Mapped[str] = mapped_column(String(18))


class NetworkSegmentAllocation(Base):
    __tablename__ = "network_segment_allocations"
    __table_args__ = (
        UniqueConstraint("environment_id", "segment_key"),
        CheckConstraint("mode IN ('ISOLATED','GROUP_LAN')", name="mode"),
        CheckConstraint("requested_hosts BETWEEN 2 AND 65534", name="hosts"),
        CheckConstraint("state IN ('RESERVED','APPLIED','RELEASING','RELEASED')", name="state"),
        Index("ix_network_segment_allocations_node_state", "node_id", "state"),
        Index(
            "uq_network_segment_allocations_active_cidr",
            "node_id",
            "cidr",
            unique=True,
            postgresql_where=text("state != 'RELEASED'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    node_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("node_network_pools.node_id"))
    environment_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("environments.id"))
    segment_key: Mapped[uuid.UUID] = mapped_column(Uuid)
    mode: Mapped[str] = mapped_column(String(16))
    requested_hosts: Mapped[int] = mapped_column(Integer)
    cidr: Mapped[str] = mapped_column(String(18))
    state: Mapped[str] = mapped_column(String(16), default="RESERVED")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
