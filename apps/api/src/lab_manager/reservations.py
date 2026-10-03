"""Transactional admission primitives; no public booking route until inventory is trusted."""

import uuid
from datetime import timedelta

from sqlalchemy import func, or_, select, text

from lab_manager.capacity import (
    CapacityPolicy,
    NodeCapacityInput,
    ResourceDemand,
    StorageObservation,
    assess_capacity,
)
from lab_manager.catalog_models import Environment
from lab_manager.nodes import NodeObservation
from lab_manager.reservation_models import (
    EnvironmentDiskAllocation,
    LessonReservation,
    NodeResourceLedger,
    NodeResourcePolicy,
)


class AdmissionRejected(Exception):
    def __init__(self, *reasons):
        self.reasons = tuple(reasons)
        super().__init__(",".join(reasons))


def unsigned(value):
    if type(value) is not int or value < 0:
        raise ValueError("Invalid inventory integer")
    return value


def node_capacity(row, storage_name, now):
    if row is None or row.payload is None:
        raise AdmissionRejected("INVENTORY_MISSING")
    try:
        sample = row.payload["sample"]
        host = sample["host"]
        storages = [item for item in sample["storages"] if item["name"] == storage_name]
        if len(storages) != 1:
            raise ValueError("Storage missing or duplicated")
        item = storages[0]
        if type(item["active"]) is not bool:
            raise ValueError("Invalid storage state")
        backend = item["backend"]
        if backend not in ("lvmthin", "dir", "zfspool"):
            raise ValueError("Unrecognized storage backend")
        metadata = item.get("thin_metadata_percent")
        if metadata is not None and (
            type(metadata) not in (int, float) or not 0 <= metadata <= 100
        ):
            raise ValueError("Invalid thin metadata")
        storage = StorageObservation(
            name=storage_name,
            total_bytes=unsigned(item["total_bytes"]),
            available_bytes=unsigned(item["available_bytes"]),
            thin_metadata_percent=metadata,
            active=item["active"],
            is_thin=backend == "lvmthin",
            commitments_reconciled=item["commitments_reconciled"] is True,
            external_committed_bytes=unsigned(item["external_committed_bytes"]),
        )
        fresh = (
            row.error_code is None
            and row.last_contact_at is not None
            and row.sample_finished_at is not None
            and -10 <= (now - row.sample_finished_at).total_seconds() <= 120
            and 0 <= (now - row.last_contact_at).total_seconds() <= 120
        )
        return NodeCapacityInput(
            memory_total_bytes=unsigned(host["memory_total_bytes"]),
            memory_free_bytes=unsigned(host["memory_free_bytes"]),
            logical_cpus=unsigned(host["logical_cpus"]),
            external_running_memory_mib=unsigned(sample["external_running_memory_mib"]),
            external_cpu_millicredits=unsigned(sample["external_cpu_millicredits"]),
            storage=storage,
            admission_ready=sample["admission_ready"] is True,
            inventory_fresh=fresh,
            ownership_reconciled=sample["ownership_reconciled"] is True,
        )
    except (KeyError, TypeError, ValueError, IndexError) as error:
        raise AdmissionRejected("INVENTORY_INCOMPLETE") from error


def capacity_policy(row):
    return CapacityPolicy(
        host_reserve_mib=row.host_reserve_mib,
        infrastructure_reserve_mib=row.infrastructure_reserve_mib,
        safety_reserve_mib=row.safety_reserve_mib,
        cpu_millicredits_per_logical_cpu=row.cpu_millicredits_per_logical_cpu,
        storage_free_percent=row.storage_free_percent,
        thin_metadata_limit_percent=row.thin_metadata_limit_percent,
    )


def peak_compute_claims(reservations, starts_at, ends_at):
    """Return the largest concurrent RAM/CPU claim in a half-open window.

    An ACTIVE lesson keeps its claim until an explicit confirmed stop, even if
    its scheduled end is in the past. RESERVED lessons claim only their booked
    interval. Adjacent intervals do not overlap.
    """
    events = []
    for item in reservations:
        start = starts_at if item.state == "ACTIVE" else max(starts_at, item.starts_at)
        end = ends_at if item.state == "ACTIVE" else min(ends_at, item.ends_at)
        if start >= end:
            continue
        events.append((start, item.memory_mib, item.cpu_millicredits))
        events.append((end, -item.memory_mib, -item.cpu_millicredits))
    memory = cpu = peak_memory = peak_cpu = 0
    for _, memory_delta, cpu_delta in sorted(events):
        memory += memory_delta
        cpu += cpu_delta
        peak_memory = max(peak_memory, memory)
        peak_cpu = max(peak_cpu, cpu)
    return peak_memory, peak_cpu


async def reserve_lesson(
    sessions,
    *,
    node_id: uuid.UUID,
    environment_id: uuid.UUID,
    teacher_id: uuid.UUID,
    request_id: uuid.UUID,
    starts_at,
    ends_at,
    demand: ResourceDemand,
):
    """Serialize admission for one node and commit compute plus disk together.

    The future API must revalidate policy, roster, environment version and
    ownership before calling this primitive. The agent currently fails closed.
    """
    if starts_at.tzinfo is None or ends_at.tzinfo is None or starts_at >= ends_at:
        raise AdmissionRejected("INVALID_WINDOW")
    if ends_at - starts_at > timedelta(hours=24):
        raise AdmissionRejected("WINDOW_TOO_LONG")
    if demand.memory_mib <= 0 or demand.cpu_millicredits <= 0 or demand.disk_bytes <= 0:
        raise AdmissionRejected("INVALID_DEMAND")
    async with sessions() as db, db.begin():
        await db.execute(text("SET LOCAL statement_timeout = '10s'"))
        await db.execute(text("SET LOCAL lock_timeout = '5s'"))
        ledger = await db.scalar(
            select(NodeResourceLedger)
            .where(NodeResourceLedger.node_id == node_id)
            .with_for_update()
        )
        if ledger is None:
            raise AdmissionRejected("NODE_POLICY_MISSING")
        now = await db.scalar(select(func.clock_timestamp()))
        environment = await db.scalar(
            select(Environment).where(Environment.id == environment_id).with_for_update()
        )
        if environment is None or environment.owner_teacher_id != teacher_id:
            raise AdmissionRejected("ENVIRONMENT_NOT_OWNED")
        existing = await db.scalar(
            select(LessonReservation).where(
                LessonReservation.teacher_id == teacher_id,
                LessonReservation.request_id == request_id,
            )
        )
        if existing is not None:
            if (
                existing.node_id == node_id
                and existing.environment_id == environment_id
                and existing.starts_at == starts_at
                and existing.ends_at == ends_at
                and existing.memory_mib == demand.memory_mib
                and existing.cpu_millicredits == demand.cpu_millicredits
                and existing.disk_bytes == demand.disk_bytes
                and existing.hibernation_bytes == demand.hibernation_bytes
            ):
                return existing
            raise AdmissionRejected("REQUEST_CONFLICT")
        if starts_at < now - timedelta(minutes=5) or ends_at <= now:
            raise AdmissionRejected("WINDOW_PAST")
        if starts_at > now + timedelta(days=90):
            raise AdmissionRejected("WINDOW_TOO_FAR")
        policy_row = await db.get(NodeResourcePolicy, node_id)
        if policy_row is None:
            raise AdmissionRejected("NODE_POLICY_MISSING")
        observation = await db.get(NodeObservation, node_id)
        node = node_capacity(observation, policy_row.storage_name, now)
        overlapping = list(
            await db.scalars(
                select(LessonReservation).where(
                    LessonReservation.node_id == node_id,
                    or_(
                        LessonReservation.state == "ACTIVE",
                        (
                            (LessonReservation.state == "RESERVED")
                            & (LessonReservation.starts_at < ends_at)
                            & (LessonReservation.ends_at > starts_at)
                        ),
                    ),
                )
            )
        )
        already_booked = await db.scalar(
            select(LessonReservation.id)
            .where(
                LessonReservation.environment_id == environment_id,
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
            raise AdmissionRejected("ENVIRONMENT_ALREADY_BOOKED")
        allocations = list(
            await db.scalars(
                select(EnvironmentDiskAllocation).where(
                    EnvironmentDiskAllocation.node_id == node_id
                )
            )
        )
        if any(item.storage_name != policy_row.storage_name for item in allocations):
            raise AdmissionRejected("STORAGE_POLICY_CHANGED")
        own_disk = await db.get(EnvironmentDiskAllocation, environment_id)
        if own_disk and own_disk.node_id != node_id:
            raise AdmissionRejected("ENVIRONMENT_BOUND_TO_OTHER_NODE")
        if own_disk and (
            own_disk.disk_bytes != demand.disk_bytes
            or own_disk.hibernation_bytes != demand.hibernation_bytes
        ):
            raise AdmissionRejected("DISK_ALLOCATION_CONFLICT")
        peak_memory, peak_cpu = peak_compute_claims(overlapping, starts_at, ends_at)
        committed = ResourceDemand(
            memory_mib=peak_memory,
            cpu_millicredits=peak_cpu,
            disk_bytes=sum(item.disk_bytes for item in allocations),
            hibernation_bytes=sum(item.hibernation_bytes for item in allocations),
        )
        requested = ResourceDemand(
            memory_mib=demand.memory_mib,
            cpu_millicredits=demand.cpu_millicredits,
            disk_bytes=0 if own_disk else demand.disk_bytes,
            hibernation_bytes=0 if own_disk else demand.hibernation_bytes,
        )
        decision = assess_capacity(
            node,
            capacity_policy(policy_row),
            committed,
            requested,
            check_current_free_memory=starts_at <= now + timedelta(minutes=5),
        )
        if not decision.permitted:
            raise AdmissionRejected(*decision.reasons)
        if own_disk is None:
            db.add(
                EnvironmentDiskAllocation(
                    environment_id=environment_id,
                    node_id=node_id,
                    storage_name=policy_row.storage_name,
                    disk_bytes=demand.disk_bytes,
                    hibernation_bytes=demand.hibernation_bytes,
                )
            )
        reservation = LessonReservation(
            node_id=node_id,
            environment_id=environment_id,
            teacher_id=teacher_id,
            request_id=request_id,
            starts_at=starts_at,
            ends_at=ends_at,
            memory_mib=demand.memory_mib,
            cpu_millicredits=demand.cpu_millicredits,
            disk_bytes=demand.disk_bytes,
            hibernation_bytes=demand.hibernation_bytes,
            state="RESERVED",
        )
        db.add(reservation)
        await db.flush()
        return reservation


async def cancel_future_lesson(sessions, *, reservation_id: uuid.UUID, teacher_id: uuid.UUID):
    """Free future compute and only an unmaterialized, otherwise unused disk hold."""
    async with sessions() as db, db.begin():
        await db.execute(text("SET LOCAL statement_timeout = '10s'"))
        await db.execute(text("SET LOCAL lock_timeout = '5s'"))
        identity = await db.get(LessonReservation, reservation_id)
        if identity is None or identity.teacher_id != teacher_id:
            raise AdmissionRejected("RESERVATION_NOT_FOUND")
        await db.scalar(
            select(NodeResourceLedger)
            .where(NodeResourceLedger.node_id == identity.node_id)
            .with_for_update()
        )
        reservation = await db.scalar(
            select(LessonReservation)
            .where(LessonReservation.id == reservation_id)
            .with_for_update()
        )
        if reservation.state == "CANCELLED":
            return reservation
        now = await db.scalar(select(func.clock_timestamp()))
        if reservation.state != "RESERVED" or reservation.starts_at <= now:
            raise AdmissionRejected("RESERVATION_ALREADY_STARTED")
        reservation.state = "CANCELLED"
        await db.flush()
        other = await db.scalar(
            select(LessonReservation.id)
            .where(
                LessonReservation.environment_id == reservation.environment_id,
                LessonReservation.state.in_(("RESERVED", "ACTIVE")),
            )
            .limit(1)
        )
        allocation = await db.get(EnvironmentDiskAllocation, reservation.environment_id)
        if other is None and allocation is not None and allocation.state == "RESERVED":
            await db.delete(allocation)
        return reservation
