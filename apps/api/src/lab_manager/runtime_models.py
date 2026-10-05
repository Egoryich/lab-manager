"""Durable lesson and guest identities; provider effects require separate receipts."""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
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


class EnvironmentRun(Base):
    __tablename__ = "environment_runs"
    __table_args__ = (
        CheckConstraint(
            "state IN ('PLANNED','PREPARING','READY','RUNNING','STOPPING','STOPPED',"
            "'RECONCILING','ERROR')",
            name="state",
        ),
        CheckConstraint("generation > 0", name="generation"),
        CheckConstraint("(state = 'STOPPED') = (stopped_at IS NOT NULL)", name="stopped_time"),
        UniqueConstraint("environment_id", "generation"),
        UniqueConstraint("id", "environment_id", name="uq_environment_runs_id_environment_id"),
        Index(
            "uq_environment_runs_unfinished",
            "environment_id",
            unique=True,
            postgresql_where=text("state != 'STOPPED'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    environment_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("environments.id"))
    node_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("node_observations.id"))
    reservation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("lesson_reservations.id"), unique=True
    )
    generation: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(20), default="PLANNED")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Runtime(Base):
    __tablename__ = "runtimes"
    __table_args__ = (
        CheckConstraint("role IN ('STUDENT','DEMO')", name="role"),
        CheckConstraint("kind IN ('LXC','QEMU')", name="kind"),
        CheckConstraint(
            "state IN ('PLANNED','PROVISIONING','STOPPED','STARTING','RUNNING',"
            "'HIBERNATING','HIBERNATED','STOPPING','RECONCILING','ERROR',"
            "'DELETING','DELETED')",
            name="state",
        ),
        CheckConstraint(
            "(role = 'STUDENT' AND student_id IS NOT NULL AND membership_generation IS NOT NULL)"
            " OR (role = 'DEMO' AND student_id IS NULL AND membership_generation IS NULL)",
            name="owner_role",
        ),
        CheckConstraint("memory_mib >= 128 AND vcpu >= 1 AND disk_gib >= 1", name="size"),
        CheckConstraint("generation > 0", name="generation"),
        CheckConstraint("(state = 'DELETED') = (deleted_at IS NOT NULL)", name="deleted_time"),
        CheckConstraint(
            "membership_generation IS NULL OR membership_generation > 0",
            name="membership_generation",
        ),
        UniqueConstraint("id", "environment_id", name="uq_runtimes_id_environment_id"),
        UniqueConstraint("id", "node_id", name="uq_runtimes_id_node_id"),
        UniqueConstraint("network_allocation_id", "guest_ipv4", name="uq_runtimes_segment_ipv4"),
        CheckConstraint(
            "(network_allocation_id IS NULL) = (guest_ipv4 IS NULL)",
            name="network_address_pair",
        ),
        Index(
            "uq_runtimes_active_student",
            "environment_id",
            "student_id",
            unique=True,
            postgresql_where=text("role = 'STUDENT' AND deleted_at IS NULL"),
        ),
        Index(
            "uq_runtimes_active_demo",
            "environment_id",
            unique=True,
            postgresql_where=text("role = 'DEMO' AND deleted_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    environment_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("environments.id"))
    node_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("node_observations.id"))
    network_allocation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("network_segment_allocations.id")
    )
    guest_ipv4: Mapped[str | None] = mapped_column(String(15))
    role: Mapped[str] = mapped_column(String(8))
    student_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"))
    membership_generation: Mapped[int | None] = mapped_column(Integer)
    profile_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("profile_versions.id"))
    kind: Mapped[str] = mapped_column(String(8))
    memory_mib: Mapped[int] = mapped_column(Integer)
    vcpu: Mapped[int] = mapped_column(Integer)
    disk_gib: Mapped[int] = mapped_column(Integer)
    generation: Mapped[int] = mapped_column(Integer, default=1)
    state: Mapped[str] = mapped_column(String(20), default="PLANNED")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProviderRuntimeBinding(Base):
    """A VMID is never ownership proof without the matching marker and node."""

    __tablename__ = "provider_runtime_bindings"
    __table_args__ = (
        UniqueConstraint("node_id", "vmid"),
        ForeignKeyConstraint(
            ["runtime_id", "node_id"],
            ["runtimes.id", "runtimes.node_id"],
            name="fk_provider_runtime_bindings_runtime_node",
        ),
        CheckConstraint("vmid BETWEEN 100 AND 999999999", name="vmid"),
        CheckConstraint("generation > 0", name="generation"),
    )

    runtime_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    node_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    vmid: Mapped[int] = mapped_column(Integer)
    ownership_marker: Mapped[str] = mapped_column(String(128))
    generation: Mapped[int] = mapped_column(Integer)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RunRuntime(Base):
    """A roster snapshot for one lesson; a Runtime persists between lessons."""

    __tablename__ = "run_runtimes"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "environment_id"],
            ["environment_runs.id", "environment_runs.environment_id"],
            name="fk_run_runtimes_run_environment",
        ),
        ForeignKeyConstraint(
            ["runtime_id", "environment_id"],
            ["runtimes.id", "runtimes.environment_id"],
            name="fk_run_runtimes_runtime_environment",
        ),
        CheckConstraint(
            "state IN ('PLANNED','READY','ACTIVE','STOPPING','STOPPED','ERROR')",
            name="state",
        ),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    runtime_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    environment_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    state: Mapped[str] = mapped_column(String(16), default="PLANNED")


class RuntimeDisk(Base):
    __tablename__ = "runtime_disks"
    __table_args__ = (
        UniqueConstraint("node_id", "storage_name", "provider_ref"),
        ForeignKeyConstraint(
            ["runtime_id", "node_id"],
            ["runtimes.id", "runtimes.node_id"],
            name="fk_runtime_disks_runtime_node",
        ),
        CheckConstraint("logical_bytes > 0 AND observed_physical_bytes >= 0", name="size"),
        CheckConstraint(
            "state IN ('PLANNED','PRESENT','UNKNOWN','DELETING','DELETED')", name="state"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    runtime_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    node_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    storage_name: Mapped[str] = mapped_column(String(64))
    provider_ref: Mapped[str | None] = mapped_column(String(256))
    logical_bytes: Mapped[int] = mapped_column(BigInteger)
    observed_physical_bytes: Mapped[int | None] = mapped_column(BigInteger)
    state: Mapped[str] = mapped_column(String(16), default="PLANNED")
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RuntimeSshCredential(Base):
    """The private key is encrypted with the VPS encryption key before storage."""

    __tablename__ = "runtime_ssh_credentials"

    runtime_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("runtimes.id"), primary_key=True)
    public_key: Mapped[str] = mapped_column(String(256))
    private_key_ciphertext: Mapped[str] = mapped_column(String(4096))
    host_key: Mapped[str | None] = mapped_column(String(2048))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
