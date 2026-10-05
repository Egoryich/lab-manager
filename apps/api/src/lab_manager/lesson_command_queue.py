"""Queue one durable LXC create command for every prepared, bound guest."""

import uuid

from sqlalchemy import func, select, text

from lab_manager.catalog_models import ProfileVersion, TemplateVersion
from lab_manager.lesson_commands import LessonCommandRejected, create_lxc_payload
from lab_manager.network_models import NetworkSegmentAllocation
from lab_manager.node_command_models import NodeCommand
from lab_manager.operation_models import Operation
from lab_manager.reservation_models import LessonReservation, NodeResourcePolicy
from lab_manager.runtime_models import (
    EnvironmentRun,
    ProviderRuntimeBinding,
    RunRuntime,
    Runtime,
    RuntimeDisk,
    RuntimeSshCredential,
)
from lab_manager.runtime_ssh import new_credential


async def queue_lxc_create_commands(sessions, *, run_id: uuid.UUID, operation_id: uuid.UUID, codec):
    """Commit credentials and all commands together; no provider call occurs here."""
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
            or run.state != "PREPARING"
            or operation is None
            or operation.environment_id != run.environment_id
            or reservation is None
            or reservation.state != "ACTIVE"
            or reservation.ends_at <= now
            or reservation.environment_id != run.environment_id
            or reservation.node_id != run.node_id
            or operation.owner_teacher_id != reservation.teacher_id
            or operation.actor_id != reservation.teacher_id
            or operation.kind != "LESSON_START"
            or operation.state != "WAITING_NODE"
        ):
            raise LessonCommandRejected("RUN_OPERATION_NOT_READY")
        runtimes = list(
            await db.scalars(
                select(Runtime)
                .join(RunRuntime, RunRuntime.runtime_id == Runtime.id)
                .where(RunRuntime.run_id == run_id)
                .order_by(Runtime.id)
                .with_for_update(of=Runtime)
            )
        )
        if not runtimes or any(runtime.node_id != run.node_id for runtime in runtimes):
            raise LessonCommandRejected("RUN_ROSTER_INVALID")
        existing = list(
            await db.scalars(
                select(NodeCommand).where(NodeCommand.parent_operation_id == operation_id)
            )
        )
        if existing:
            if len(existing) != len(runtimes) or {c.runtime_id for c in existing} != {
                runtime.id for runtime in runtimes
            }:
                raise LessonCommandRejected("COMMAND_ROSTER_CHANGED")
            return existing
        policy = await db.get(NodeResourcePolicy, run.node_id)
        if policy is None:
            raise LessonCommandRejected("NODE_POLICY_MISSING")
        commands = []
        for runtime in runtimes:
            binding = await db.get(ProviderRuntimeBinding, runtime.id)
            disks = list(
                await db.scalars(select(RuntimeDisk).where(RuntimeDisk.runtime_id == runtime.id))
            )
            allocation = (
                await db.get(NetworkSegmentAllocation, runtime.network_allocation_id)
                if runtime.network_allocation_id is not None
                else None
            )
            profile = await db.get(ProfileVersion, runtime.profile_version_id)
            template = (
                await db.get(TemplateVersion, profile.template_version_id) if profile else None
            )
            if (
                binding is None
                or len(disks) != 1
                or disks[0].logical_bytes != runtime.disk_gib * 1024**3
                or disks[0].node_id != run.node_id
                or disks[0].storage_name != policy.storage_name
                or disks[0].state != "PLANNED"
                or allocation is None
                or template is None
            ):
                raise LessonCommandRejected("LXC_DEPENDENCIES_NOT_READY")
            credential = await db.get(RuntimeSshCredential, runtime.id)
            if credential is None:
                credential = new_credential(runtime.id, codec)
                db.add(credential)
            command_id = uuid.uuid4()
            payload = create_lxc_payload(
                operation_id=command_id,
                runtime=runtime,
                binding=binding,
                allocation=allocation,
                template=template,
                storage_name=policy.storage_name,
                ssh_public_key=credential.public_key,
            )
            commands.append(
                NodeCommand(
                    id=command_id,
                    parent_operation_id=operation_id,
                    runtime_id=runtime.id,
                    node_id=run.node_id,
                    kind="LXC_CREATE",
                    vmid=binding.vmid,
                    generation=runtime.generation,
                    payload=payload,
                    state="QUEUED",
                )
            )
        db.add_all(commands)
        for runtime in runtimes:
            runtime.state = "PROVISIONING"
        await db.flush()
        return commands
