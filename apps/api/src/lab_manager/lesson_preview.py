"""Read-only lesson capacity preview; booking must recalculate under a node lock."""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import func, or_, select

from lab_manager.capacity import ResourceDemand, assess_capacity
from lab_manager.catalog import estimate
from lab_manager.catalog_models import Environment, ProfileVersion, TemplateVersion
from lab_manager.catalog_schemas import MachineSizing, ResourceTotal
from lab_manager.dependencies import DB, Actor, Problem, require_role
from lab_manager.models import Group
from lab_manager.nodes import NodeObservation
from lab_manager.permissions import effective_policy, profile_allowed
from lab_manager.reservation_models import (
    EnvironmentDiskAllocation,
    LessonReservation,
    NodeResourcePolicy,
)
from lab_manager.reservations import (
    AdmissionRejected,
    capacity_policy,
    node_capacity,
    peak_compute_claims,
)

router = APIRouter(tags=["Lessons"])


class NodeCapacityPreview(BaseModel):
    node_id: uuid.UUID
    node_name: str
    storage_name: str
    available: bool
    reasons: list[str]
    ram_headroom_mib: int | None = None
    cpu_headroom_millicredits: int | None = None
    storage_headroom_bytes: int | None = None


class LessonPreview(BaseModel):
    environment_id: uuid.UUID
    group_version: int
    student_count: int
    total: ResourceTotal
    within_per_environment_limits: bool
    starts_at: datetime
    ends_at: datetime
    reasons: list[str]
    nodes: list[NodeCapacityPreview]
    reservation_created: bool = False


def demand_from_total(total: ResourceTotal) -> ResourceDemand:
    return ResourceDemand(
        memory_mib=total.memory_mib,
        cpu_millicredits=total.cpu_millicredits,
        disk_bytes=total.disk_bytes,
        hibernation_bytes=total.hibernation_bytes,
    )


async def preview_node(db, row, observation, demand, starts_at, ends_at, now, blockers):
    reasons = list(blockers)
    if observation is None:
        reasons.append("INVENTORY_MISSING")
        return NodeCapacityPreview(
            node_id=row.node_id,
            node_name="Недоступный узел",
            storage_name=row.storage_name,
            available=False,
            reasons=list(dict.fromkeys(reasons)),
        )
    if (
        observation.payload
        and observation.payload.get("sample", {}).get("admission_ready") is False
    ):
        reasons.append("NODE_NOT_ADMISSION_READY")
    try:
        capacity = node_capacity(observation, row.storage_name, now)
    except AdmissionRejected as error:
        reasons.extend(error.reasons)
        return NodeCapacityPreview(
            node_id=row.node_id,
            node_name=observation.name,
            storage_name=row.storage_name,
            available=False,
            reasons=list(dict.fromkeys(reasons)),
        )
    overlapping = list(
        await db.scalars(
            select(LessonReservation).where(
                LessonReservation.node_id == row.node_id,
                or_(
                    LessonReservation.state == "ACTIVE",
                    (LessonReservation.state == "RESERVED")
                    & (LessonReservation.starts_at < ends_at)
                    & (LessonReservation.ends_at > starts_at),
                ),
            )
        )
    )
    already_booked = await db.scalar(
        select(LessonReservation.id)
        .where(
            LessonReservation.environment_id == demand.environment_id,
            or_(
                LessonReservation.state == "ACTIVE",
                (LessonReservation.state == "RESERVED")
                & (LessonReservation.starts_at < ends_at)
                & (LessonReservation.ends_at > starts_at),
            ),
        )
        .limit(1)
    )
    if already_booked is not None:
        reasons.append("ENVIRONMENT_ALREADY_BOOKED")
    allocations = list(
        await db.scalars(
            select(EnvironmentDiskAllocation).where(
                EnvironmentDiskAllocation.node_id == row.node_id
            )
        )
    )
    if any(item.storage_name != row.storage_name for item in allocations):
        reasons.append("STORAGE_POLICY_CHANGED")
    own = await db.get(EnvironmentDiskAllocation, demand.environment_id)
    if own and own.node_id != row.node_id:
        reasons.append("ENVIRONMENT_BOUND_TO_OTHER_NODE")
        own = None
    if own and (
        own.disk_bytes != demand.resources.disk_bytes
        or own.hibernation_bytes != demand.resources.hibernation_bytes
    ):
        reasons.append("DISK_ALLOCATION_CONFLICT")
    peak_ram, peak_cpu = peak_compute_claims(overlapping, starts_at, ends_at)
    committed = ResourceDemand(
        memory_mib=peak_ram,
        cpu_millicredits=peak_cpu,
        disk_bytes=sum(item.disk_bytes for item in allocations),
        hibernation_bytes=sum(item.hibernation_bytes for item in allocations),
    )
    requested = ResourceDemand(
        memory_mib=demand.resources.memory_mib,
        cpu_millicredits=demand.resources.cpu_millicredits,
        disk_bytes=0 if own else demand.resources.disk_bytes,
        hibernation_bytes=0 if own else demand.resources.hibernation_bytes,
    )
    decision = assess_capacity(
        capacity,
        capacity_policy(row),
        committed,
        requested,
        check_current_free_memory=starts_at <= now + timedelta(minutes=5),
    )
    reasons.extend(decision.reasons)
    return NodeCapacityPreview(
        node_id=row.node_id,
        node_name=observation.name,
        storage_name=row.storage_name,
        available=not reasons,
        reasons=list(dict.fromkeys(reasons)),
        ram_headroom_mib=decision.ram_headroom_mib,
        cpu_headroom_millicredits=decision.cpu_headroom_millicredits,
        storage_headroom_bytes=decision.storage_headroom_bytes,
    )


@dataclass(frozen=True)
class PreviewDemand:
    environment_id: uuid.UUID
    resources: ResourceDemand


@router.get("/environments/{environment_id}/lesson-preview", response_model=LessonPreview)
async def preview_lesson(
    environment_id: uuid.UUID, starts_at: datetime, ends_at: datetime, actor: Actor, db: DB
):
    require_role(actor, "TEACHER")
    environment = await db.get(Environment, environment_id)
    if environment is None or environment.owner_teacher_id != actor.id:
        raise Problem(404, "NOT_FOUND", "Окружение не найдено.")
    now = await db.scalar(select(func.clock_timestamp()))
    if (
        starts_at.tzinfo is None
        or ends_at.tzinfo is None
        or starts_at >= ends_at
        or ends_at - starts_at > timedelta(hours=24)
        or starts_at < now - timedelta(minutes=5)
        or ends_at <= now
        or starts_at > now + timedelta(days=90)
    ):
        raise Problem(422, "INVALID_WINDOW", "Укажите будущий интервал занятия до 24 часов.")
    group = await db.get(Group, environment.group_id)
    policy = await effective_policy(db, actor.id)
    student = await db.get(ProfileVersion, environment.profile_version_id)
    demo = await db.get(ProfileVersion, environment.demo_profile_version_id)
    student_template = await db.get(TemplateVersion, student.template_version_id)
    demo_template = await db.get(TemplateVersion, demo.template_version_id)
    student_size = MachineSizing(
        memory_mib=environment.student_memory_mib,
        vcpu=environment.student_vcpu,
        disk_gib=environment.student_disk_gib,
    )
    demo_size = MachineSizing(
        memory_mib=environment.demo_memory_mib,
        vcpu=environment.demo_vcpu,
        disk_gib=environment.demo_disk_gib,
    )
    plan = await estimate(
        db, group, student, student_template, demo, demo_template, policy, student_size, demo_size
    )
    blockers = []
    if group.archived_at:
        blockers.append("GROUP_ARCHIVED")
    if not profile_allowed(student, student_template, policy) or not profile_allowed(
        demo, demo_template, policy, demo=True
    ):
        blockers.append("PROFILE_FORBIDDEN")
    if not plan.within_per_environment_limits:
        blockers.append("QUOTA_EXCEEDED")
    if plan.student_count == 0:
        blockers.append("GROUP_EMPTY")
    rows = list(await db.scalars(select(NodeResourcePolicy).order_by(NodeResourcePolicy.node_id)))
    demand = PreviewDemand(environment.id, demand_from_total(plan.total))
    nodes = [
        await preview_node(
            db,
            row,
            await db.get(NodeObservation, row.node_id),
            demand,
            starts_at,
            ends_at,
            now,
            blockers,
        )
        for row in rows
    ]
    if not nodes:
        blockers.append("NODE_POLICY_MISSING")
    return LessonPreview(
        environment_id=environment.id,
        group_version=group.version,
        student_count=plan.student_count,
        total=plan.total,
        within_per_environment_limits=plan.within_per_environment_limits,
        starts_at=starts_at,
        ends_at=ends_at,
        reasons=blockers,
        nodes=nodes,
    )
