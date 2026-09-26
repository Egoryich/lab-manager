"""Immutable catalog and permission revisions; environment preparation."""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from lab_manager.models import Base


class TemplateVersion(Base):
    __tablename__ = "template_versions"
    __table_args__ = (
        CheckConstraint("runtime_kind IN ('LXC','QEMU')", name="kind"),
        CheckConstraint("guest_family IN ('LINUX','WINDOWS')", name="family"),
        CheckConstraint("runtime_kind != 'LXC' OR guest_family = 'LINUX'", name="lxc_linux"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120))
    version_label: Mapped[str] = mapped_column(String(64))
    runtime_kind: Mapped[str] = mapped_column(String(8))
    guest_family: Mapped[str] = mapped_column(String(16))
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProfileVersion(Base):
    __tablename__ = "profile_versions"
    __table_args__ = (
        CheckConstraint(
            "memory_mib > 0 AND vcpu > 0 AND cpu_millicredits > 0 AND disk_gib > 0",
            name="resources",
        ),
        CheckConstraint("network_mode IN ('ISOLATED','GROUP_LAN')", name="network"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120))
    template_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("template_versions.id"))
    memory_mib: Mapped[int] = mapped_column(Integer)
    vcpu: Mapped[int] = mapped_column(Integer)
    cpu_millicredits: Mapped[int] = mapped_column(Integer)
    disk_gib: Mapped[int] = mapped_column(Integer)
    network_mode: Mapped[str] = mapped_column(String(16))
    internet_enabled: Mapped[bool] = mapped_column(Boolean)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PermissionPolicyRevision(Base):
    __tablename__ = "permission_policy_revisions"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120))
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PolicyPermission(Base):
    __tablename__ = "policy_permissions"
    revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("permission_policy_revisions.id"), primary_key=True
    )
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    allowed: Mapped[bool] = mapped_column(Boolean)


class PolicyLimit(Base):
    __tablename__ = "policy_limits"
    __table_args__ = (CheckConstraint("value >= 0", name="nonnegative"),)
    revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("permission_policy_revisions.id"), primary_key=True
    )
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[int] = mapped_column(BigInteger)


class DemoProfileGrant(Base):
    __tablename__ = "demo_profile_grants"
    revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("permission_policy_revisions.id"), primary_key=True
    )
    profile_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("profile_versions.id"), primary_key=True
    )


class TeacherPolicyAssignment(Base):
    __tablename__ = "teacher_policy_assignments"
    teacher_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    revision_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("permission_policy_revisions.id"))
    version: Mapped[int] = mapped_column(Integer, default=1)


class Environment(Base):
    __tablename__ = "environments"
    __table_args__ = (UniqueConstraint("owner_teacher_id", "request_id"),)
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120))
    group_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("groups.id"), index=True)
    owner_teacher_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    profile_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("profile_versions.id"))
    demo_profile_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("profile_versions.id"))
    permission_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("permission_policy_revisions.id")
    )
    request_id: Mapped[uuid.UUID]
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
