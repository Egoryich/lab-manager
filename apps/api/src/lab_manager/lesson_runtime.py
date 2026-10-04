"""Atomically freeze a booked lesson's roster into persistent guest identities.

This is a domain step, not a provider call. The command producer must only
submit guests after the run, resource claim and network claims have committed.
"""

import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select, text

from lab_manager.catalog import configuration, estimate
from lab_manager.catalog_models import Environment
from lab_manager.dependencies import Problem
from lab_manager.lesson_booking import environment_body
from lab_manager.lesson_network import claim_run_networks
from lab_manager.models import GroupMember, User, UserRole
from lab_manager.network_allocations import NetworkAllocationRejected
from lab_manager.nodes import NodeObservation
from lab_manager.reservation_models import (
    EnvironmentDiskAllocation,
    LessonReservation,
    NodeResourceLedger,
    NodeResourcePolicy,
)
from lab_manager.reservations import AdmissionRejected, node_capacity
from lab_manager.runtime_models import EnvironmentRun, RunRuntime, Runtime


class RunPreparationRejected(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Teacher:
    id: uuid.UUID
    roles: tuple[str, ...] = ("TEACHER",)


def matches(runtime, *, node_id, profile_id, kind, memory_mib, vcpu, disk_gib, generation):
    return (
        runtime.node_id == node_id
        and runtime.profile_version_id == profile_id
        and runtime.kind == kind
        and runtime.memory_mib == memory_mib
        and runtime.vcpu == vcpu
        and runtime.disk_gib == disk_gib
        and runtime.membership_generation == generation
        and (generation is None or runtime.generation == generation)
        and runtime.state in ("PLANNED", "STOPPED")
    )


async def prepare_reserved_run(sessions, *, reservation_id: uuid.UUID, teacher_id: uuid.UUID):
    """Create one run and roster snapshot; return the existing run on retry.

    The node ledger lock is the same serialization point as booking/cancel.
    Starting a lesson keeps its compute reservation ACTIVE until a later,
    confirmed shutdown; no guest is created by this function.
    """
    async with sessions() as db, db.begin():
        await db.execute(text("SET LOCAL statement_timeout = '10s'"))
        await db.execute(text("SET LOCAL lock_timeout = '5s'"))
        identity = await db.get(LessonReservation, reservation_id)
        if identity is None or identity.teacher_id != teacher_id:
            raise RunPreparationRejected("RESERVATION_NOT_FOUND")
        ledger = await db.scalar(
            select(NodeResourceLedger)
            .where(NodeResourceLedger.node_id == identity.node_id)
            .with_for_update()
        )
        if ledger is None:
            raise RunPreparationRejected("NODE_POLICY_MISSING")
        teacher = await db.scalar(select(User).where(User.id == teacher_id).with_for_update())
        if teacher is None or teacher.status != "ACTIVE":
            raise RunPreparationRejected("TEACHER_NOT_ACTIVE")
        teacher_role = await db.get(UserRole, (teacher_id, "TEACHER"))
        if teacher_role is None:
            raise RunPreparationRejected("TEACHER_NOT_ACTIVE")
        environment = await db.get(Environment, identity.environment_id)
        if environment is None or environment.owner_teacher_id != teacher_id:
            raise RunPreparationRejected("ENVIRONMENT_NOT_OWNED")
        try:
            group, policy, selected, student_size, demo_size = await configuration(
                db, Teacher(teacher_id), environment_body(environment), lock=True
            )
        except Problem as error:
            raise RunPreparationRejected(error.code) from error
        await db.refresh(environment, with_for_update=True)
        reservation = await db.scalar(
            select(LessonReservation)
            .where(LessonReservation.id == reservation_id)
            .with_for_update()
        )
        await db.refresh(reservation, with_for_update=True)
        existing = await db.scalar(
            select(EnvironmentRun).where(EnvironmentRun.reservation_id == reservation_id)
        )
        if existing is not None:
            if reservation.state != "ACTIVE":
                raise RunPreparationRejected("RUN_RESERVATION_MISMATCH")
            return existing
        now = await db.scalar(select(func.clock_timestamp()))
        if reservation.state != "RESERVED":
            raise RunPreparationRejected("RESERVATION_NOT_READY")
        if reservation.starts_at > now + timedelta(minutes=30) or reservation.ends_at <= now:
            raise RunPreparationRejected("OUTSIDE_LESSON_WINDOW")
        if environment.group_id != group.id or reservation.environment_id != environment.id:
            raise RunPreparationRejected("ENVIRONMENT_CHANGED")
        plan = await estimate(db, group, *selected, policy, student_size, demo_size)
        if not plan.within_per_environment_limits or plan.student_count < 1:
            raise RunPreparationRejected("ROSTER_OR_QUOTA_CHANGED")
        if (
            plan.total.memory_mib != reservation.memory_mib
            or plan.total.cpu_millicredits != reservation.cpu_millicredits
            or plan.total.disk_bytes != reservation.disk_bytes
            or plan.total.hibernation_bytes != reservation.hibernation_bytes
        ):
            raise RunPreparationRejected("BOOKED_DEMAND_CHANGED")
        observation = await db.get(NodeObservation, reservation.node_id)
        node_policy = await db.get(NodeResourcePolicy, reservation.node_id)
        if node_policy is None:
            raise RunPreparationRejected("NODE_POLICY_MISSING")
        allocation = await db.get(EnvironmentDiskAllocation, environment.id)
        if (
            allocation is None
            or allocation.node_id != reservation.node_id
            or allocation.storage_name != node_policy.storage_name
            or allocation.disk_bytes != reservation.disk_bytes
            or allocation.hibernation_bytes != reservation.hibernation_bytes
        ):
            raise RunPreparationRejected("DISK_ALLOCATION_CONFLICT")
        try:
            capacity = node_capacity(observation, node_policy.storage_name, now)
        except AdmissionRejected as error:
            raise RunPreparationRejected(error.reasons[0]) from error
        if not (
            capacity.inventory_fresh
            and capacity.admission_ready
            and capacity.ownership_reconciled
            and capacity.storage.active
            and capacity.storage.commitments_reconciled
        ):
            raise RunPreparationRejected("NODE_NOT_READY")
        storage = capacity.storage
        if (
            storage.total_bytes is None
            or storage.available_bytes is None
            or storage.available_bytes * 100
            < storage.total_bytes * node_policy.storage_free_percent
            or (
                storage.is_thin
                and (
                    storage.thin_metadata_percent is None
                    or storage.thin_metadata_percent >= node_policy.thin_metadata_limit_percent
                )
            )
        ):
            raise RunPreparationRejected("STORAGE_UNSAFE")
        unfinished = await db.scalar(
            select(EnvironmentRun.id)
            .where(
                EnvironmentRun.environment_id == environment.id,
                EnvironmentRun.state != "STOPPED",
            )
            .limit(1)
        )
        if unfinished is not None:
            raise RunPreparationRejected("ENVIRONMENT_ALREADY_RUNNING")
        generation = (
            await db.scalar(
                select(func.coalesce(func.max(EnvironmentRun.generation), 0)).where(
                    EnvironmentRun.environment_id == environment.id
                )
            )
        ) + 1
        members = list(
            await db.scalars(
                select(GroupMember)
                .where(GroupMember.group_id == group.id, GroupMember.status == "ACTIVE")
                .order_by(GroupMember.student_id)
            )
        )
        if len(members) != plan.student_count:
            raise RunPreparationRejected("ROSTER_CHANGED")
        existing_runtimes = list(
            await db.scalars(
                select(Runtime).where(
                    Runtime.environment_id == environment.id,
                    Runtime.deleted_at.is_(None),
                )
            )
        )
        students = {item.student_id: item for item in existing_runtimes if item.role == "STUDENT"}
        demo = next((item for item in existing_runtimes if item.role == "DEMO"), None)
        if len(students) != sum(item.role == "STUDENT" for item in existing_runtimes):
            raise RunPreparationRejected("RUNTIME_DUPLICATE")
        if set(students) - {member.student_id for member in members}:
            raise RunPreparationRejected("RUNTIME_MEMBERSHIP_CHANGED")
        run = EnvironmentRun(
            environment_id=environment.id,
            node_id=reservation.node_id,
            reservation_id=reservation.id,
            generation=generation,
            state="PREPARING",
        )
        db.add(run)
        await db.flush()
        roster = []
        student_profile, student_template, demo_profile, demo_template = selected
        for member in members:
            runtime = students.get(member.student_id)
            if runtime is None:
                runtime = Runtime(
                    environment_id=environment.id,
                    node_id=reservation.node_id,
                    role="STUDENT",
                    student_id=member.student_id,
                    membership_generation=member.generation,
                    profile_version_id=student_profile.id,
                    kind=student_template.runtime_kind,
                    memory_mib=student_size.memory_mib,
                    vcpu=student_size.vcpu,
                    disk_gib=student_size.disk_gib,
                    generation=member.generation,
                    state="PLANNED",
                )
                db.add(runtime)
            elif not matches(
                runtime,
                node_id=reservation.node_id,
                profile_id=student_profile.id,
                kind=student_template.runtime_kind,
                memory_mib=student_size.memory_mib,
                vcpu=student_size.vcpu,
                disk_gib=student_size.disk_gib,
                generation=member.generation,
            ):
                raise RunPreparationRejected("RUNTIME_CONFIGURATION_CHANGED")
            roster.append(runtime)
        if demo is None:
            demo = Runtime(
                environment_id=environment.id,
                node_id=reservation.node_id,
                role="DEMO",
                student_id=None,
                membership_generation=None,
                profile_version_id=demo_profile.id,
                kind=demo_template.runtime_kind,
                memory_mib=demo_size.memory_mib,
                vcpu=demo_size.vcpu,
                disk_gib=demo_size.disk_gib,
                generation=1,
                state="PLANNED",
            )
            db.add(demo)
        elif not matches(
            demo,
            node_id=reservation.node_id,
            profile_id=demo_profile.id,
            kind=demo_template.runtime_kind,
            memory_mib=demo_size.memory_mib,
            vcpu=demo_size.vcpu,
            disk_gib=demo_size.disk_gib,
            generation=None,
        ):
            raise RunPreparationRejected("RUNTIME_CONFIGURATION_CHANGED")
        roster.append(demo)
        await db.flush()
        try:
            await claim_run_networks(
                db,
                environment_id=environment.id,
                node_id=reservation.node_id,
                runtimes=roster,
                profiles={student_profile.id: student_profile, demo_profile.id: demo_profile},
            )
        except NetworkAllocationRejected as error:
            raise RunPreparationRejected(error.reason) from error
        db.add_all(
            RunRuntime(
                run_id=run.id, runtime_id=runtime.id, environment_id=environment.id, state="PLANNED"
            )
            for runtime in roster
        )
        reservation.state = "ACTIVE"
        return run
