"""Queue one fenced LXC start per created guest after all gateways are ready."""

import asyncio
import hashlib
import logging
import uuid

from sqlalchemy import func, select, text

from lab_manager.network_models import NetworkSegmentAllocation
from lab_manager.node_command_models import NodeCommand
from lab_manager.node_segments import NodeSegmentClient, NodeSegmentError
from lab_manager.operation_models import Operation
from lab_manager.reservation_models import LessonReservation
from lab_manager.runtime_models import (
    EnvironmentRun,
    ProviderRuntimeBinding,
    RunRuntime,
    Runtime,
    RuntimeDisk,
)

logger = logging.getLogger("lab_manager.lesson_lxc_start")


async def queue_start_commands(sessions, *, run_id: uuid.UUID, operation_id: uuid.UUID) -> bool:
    """Freeze an all-or-none start roster; a retry never creates another command."""
    async with sessions() as db, db.begin():
        await db.execute(text("SET LOCAL statement_timeout = '10s'"))
        await db.execute(text("SET LOCAL lock_timeout = '5s'"))
        run = await db.scalar(
            select(EnvironmentRun).where(EnvironmentRun.id == run_id).with_for_update()
        )
        operation = await db.get(Operation, operation_id)
        reservation = await db.get(LessonReservation, run.reservation_id) if run else None
        now = await db.scalar(select(func.clock_timestamp()))
        if (
            run is None
            or run.state != "READY"
            or operation is None
            or operation.environment_id != run.environment_id
            or operation.kind != "LESSON_START"
            or operation.state != "WAITING_NODE"
            or operation.request_digest
            != hashlib.sha256(f"LESSON_START:{run.reservation_id}".encode()).hexdigest()
            or reservation is None
            or reservation.state != "ACTIVE"
            or reservation.ends_at <= now
            or reservation.environment_id != run.environment_id
            or reservation.node_id != run.node_id
            or operation.owner_teacher_id != reservation.teacher_id
            or operation.actor_id != reservation.teacher_id
        ):
            return False
        members = list(await db.scalars(select(RunRuntime).where(RunRuntime.run_id == run.id)))
        runtimes = list(
            await db.scalars(
                select(Runtime)
                .join(RunRuntime, RunRuntime.runtime_id == Runtime.id)
                .where(RunRuntime.run_id == run.id)
                .order_by(Runtime.id)
                .with_for_update(of=Runtime)
            )
        )
        if (
            not members
            or len(members) != len(runtimes)
            or {member.runtime_id for member in members} != {item.id for item in runtimes}
            or any(member.state != "READY" for member in members)
        ):
            return False
        existing = list(
            await db.scalars(
                select(NodeCommand).where(
                    NodeCommand.parent_operation_id == operation_id,
                    NodeCommand.kind == "LXC_START",
                )
            )
        )
        if existing:
            if len(existing) != len(runtimes) or {item.runtime_id for item in existing} != {
                runtime.id for runtime in runtimes
            }:
                logger.error("lesson_start_command_roster_mismatch run=%s", run.id)
            return False
        commands = []
        for runtime in runtimes:
            binding = await db.get(ProviderRuntimeBinding, runtime.id)
            allocation = (
                await db.get(NetworkSegmentAllocation, runtime.network_allocation_id)
                if runtime.network_allocation_id
                else None
            )
            disks = list(
                await db.scalars(select(RuntimeDisk).where(RuntimeDisk.runtime_id == runtime.id))
            )
            created = list(
                await db.scalars(
                    select(NodeCommand).where(
                        NodeCommand.parent_operation_id == operation_id,
                        NodeCommand.runtime_id == runtime.id,
                        NodeCommand.kind == "LXC_CREATE",
                    )
                )
            )
            if (
                runtime.node_id != run.node_id
                or runtime.environment_id != run.environment_id
                or runtime.kind != "LXC"
                or runtime.state != "STOPPED"
                or binding is None
                or binding.node_id != run.node_id
                or binding.generation != runtime.generation
                or binding.ownership_marker
                != f"lab-manager:runtime={runtime.id};generation={runtime.generation}"
                or allocation is None
                or allocation.node_id != run.node_id
                or allocation.environment_id != run.environment_id
                or allocation.state != "APPLIED"
                or allocation.mode != "ISOLATED"
                or len(disks) != 1
                or disks[0].state != "PRESENT"
                or disks[0].node_id != run.node_id
                or len(created) != 1
                or created[0].state != "SUCCEEDED"
                or created[0].node_id != run.node_id
                or created[0].vmid != binding.vmid
                or created[0].generation != runtime.generation
            ):
                return False
            command_id = uuid.uuid4()
            payload = {
                "operation_id": str(command_id),
                "node_id": str(run.node_id),
                "kind": "LXC_START",
                "runtime_id": str(runtime.id),
                "generation": runtime.generation,
                "vmid": binding.vmid,
            }
            commands.append(
                NodeCommand(
                    id=command_id,
                    parent_operation_id=operation_id,
                    runtime_id=runtime.id,
                    node_id=run.node_id,
                    kind="LXC_START",
                    vmid=binding.vmid,
                    generation=runtime.generation,
                    payload=payload,
                    state="QUEUED",
                )
            )
        db.add_all(commands)
        for runtime in runtimes:
            runtime.state = "STARTING"
        await db.flush()
        return True


async def process(sessions, endpoints, *, client_factory=NodeSegmentClient) -> bool:
    """A live owned gateway is required before any LXC_START is committed."""
    if not endpoints:
        return False
    endpoint_by_id = {endpoint.id: endpoint for endpoint in endpoints}
    async with sessions() as db:
        candidates = list(
            await db.execute(
                select(EnvironmentRun.id, Operation.id, EnvironmentRun.node_id)
                .select_from(EnvironmentRun)
                .join(Operation, Operation.environment_id == EnvironmentRun.environment_id)
                .where(
                    EnvironmentRun.state == "READY",
                    EnvironmentRun.node_id.in_(endpoint_by_id),
                    Operation.kind == "LESSON_START",
                    Operation.state == "WAITING_NODE",
                )
                .order_by(EnvironmentRun.created_at, EnvironmentRun.id)
                .limit(10)
            )
        )
    for run_id, operation_id, node_id in candidates:
        async with sessions() as db:
            allocations = list(
                await db.scalars(
                    select(NetworkSegmentAllocation)
                    .join(Runtime, Runtime.network_allocation_id == NetworkSegmentAllocation.id)
                    .join(RunRuntime, RunRuntime.runtime_id == Runtime.id)
                    .where(RunRuntime.run_id == run_id)
                    .distinct()
                )
            )
        if not allocations:
            continue
        client = client_factory(endpoint_by_id[node_id])
        try:
            async with asyncio.timeout(25):
                for allocation in allocations:
                    if allocation.state != "APPLIED" or allocation.node_id != node_id:
                        raise NodeSegmentError("SEGMENT_NOT_READY")
                    observed = await asyncio.to_thread(client.status, allocation.id)
                    if (
                        observed is None
                        or observed["state"] != "ACTIVE"
                        or observed["mode"] != allocation.mode
                        or observed["cidr"] != allocation.cidr
                    ):
                        raise NodeSegmentError("SEGMENT_GATEWAY_NOT_READY")
        except (NodeSegmentError, TimeoutError) as error:
            logger.warning(
                "lesson_start_gateway_unavailable run=%s code=%s",
                run_id,
                getattr(error, "code", "NODE_SEGMENT_TIMEOUT"),
            )
            continue
        if await queue_start_commands(sessions, run_id=run_id, operation_id=operation_id):
            return True
    return False
