"""Confirm a created LXC and its disk from a fresh node observation."""

import re
import uuid

from sqlalchemy import func, select, text

from lab_manager.node_command_models import NodeCommand
from lab_manager.nodes import NodeObservation
from lab_manager.operation_models import Operation
from lab_manager.runtime_models import (
    EnvironmentRun,
    ProviderRuntimeBinding,
    RunRuntime,
    Runtime,
    RuntimeDisk,
)


class CreateReconciliationError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def disk_reference(command, runtime, binding, disk, sample: dict) -> str:
    """Require one matching owned guest and one full-size root volume."""
    marker = f"lab-manager:runtime={runtime.id};generation={runtime.generation}"
    if (
        command.state != "SUCCEEDED"
        or command.kind != "LXC_CREATE"
        or command.runtime_id != runtime.id
        or command.node_id != runtime.node_id
        or command.vmid != binding.vmid
        or command.generation != runtime.generation
        or binding.runtime_id != runtime.id
        or binding.node_id != runtime.node_id
        or binding.ownership_marker != marker
        or runtime.state != "PROVISIONING"
        or disk.runtime_id != runtime.id
        or disk.node_id != runtime.node_id
        or disk.state != "PLANNED"
        or disk.provider_ref is not None
        or not isinstance(sample, dict)
        or sample.get("ownership_reconciled") is not True
    ):
        raise CreateReconciliationError("CREATE_IDENTITY_MISMATCH")
    guests = [guest for guest in sample.get("guests", []) if guest.get("vmid") == binding.vmid]
    if len(guests) != 1 or any(
        guest.get(key) != expected
        for guest in guests
        for key, expected in (
            ("kind", "LXC"),
            ("reported_status", "stopped"),
            ("ownership_marker", marker),
        )
    ):
        raise CreateReconciliationError("CREATE_GUEST_NOT_OBSERVED")
    pools = [
        pool
        for pool in sample.get("local_thin_pools", [])
        if pool.get("storage") == disk.storage_name
    ]
    if len(pools) != 1 or not isinstance(pools[0].get("volumes"), list):
        raise CreateReconciliationError("CREATE_STORAGE_NOT_OBSERVED")
    expected_name = re.compile(rf"vm-{binding.vmid}-disk-[0-9]+\Z")
    volumes = [
        volume
        for volume in pools[0]["volumes"]
        if isinstance(volume, dict)
        and isinstance(volume.get("name"), str)
        and expected_name.fullmatch(volume["name"])
        and volume.get("size_bytes") == disk.logical_bytes
    ]
    if len(volumes) != 1:
        raise CreateReconciliationError("CREATE_DISK_NOT_OBSERVED")
    return f"{disk.storage_name}:{volumes[0]['name']}"


async def reconcile_created_run(sessions, *, run_id: uuid.UUID, operation_id: uuid.UUID) -> bool:
    """Make the whole roster READY together; missing evidence leaves it unchanged."""
    async with sessions() as db, db.begin():
        await db.execute(text("SET LOCAL statement_timeout = '10s'"))
        await db.execute(text("SET LOCAL lock_timeout = '5s'"))
        run = await db.scalar(
            select(EnvironmentRun).where(EnvironmentRun.id == run_id).with_for_update()
        )
        operation = await db.get(Operation, operation_id)
        if (
            run is None
            or run.state != "PREPARING"
            or operation is None
            or operation.environment_id != run.environment_id
            or operation.kind != "LESSON_START"
            or operation.state != "WAITING_NODE"
        ):
            return False
        observation = await db.get(NodeObservation, run.node_id)
        now = await db.scalar(select(func.clock_timestamp()))
        if (
            observation is None
            or observation.error_code is not None
            or observation.payload is None
            or observation.last_contact_at is None
            or observation.sample_finished_at is None
            or not 0 <= (now - observation.last_contact_at).total_seconds() <= 120
            or not -10 <= (now - observation.sample_finished_at).total_seconds() <= 120
        ):
            return False
        commands = list(
            await db.scalars(
                select(NodeCommand).where(NodeCommand.parent_operation_id == operation_id)
            )
        )
        runtimes = list(
            await db.scalars(
                select(Runtime)
                .join(RunRuntime, RunRuntime.runtime_id == Runtime.id)
                .where(RunRuntime.run_id == run_id)
                .order_by(Runtime.id)
                .with_for_update(of=Runtime)
            )
        )
        run_roster = list(
            await db.scalars(
                select(RunRuntime).where(RunRuntime.run_id == run_id).with_for_update()
            )
        )
        if (
            not runtimes
            or len(run_roster) != len(runtimes)
            or len(commands) != len(runtimes)
            or {command.runtime_id for command in commands} != {runtime.id for runtime in runtimes}
            or any(command.finished_at is None for command in commands)
            or any(observation.last_contact_at < command.finished_at for command in commands)
        ):
            return False
        by_command = {command.runtime_id: command for command in commands}
        confirmed = []
        for runtime in runtimes:
            binding = await db.get(ProviderRuntimeBinding, runtime.id)
            disks = list(
                await db.scalars(
                    select(RuntimeDisk)
                    .where(RuntimeDisk.runtime_id == runtime.id)
                    .with_for_update()
                )
            )
            if binding is None or len(disks) != 1:
                return False
            try:
                reference = disk_reference(
                    by_command[runtime.id],
                    runtime,
                    binding,
                    disks[0],
                    observation.payload["sample"],
                )
            except (CreateReconciliationError, KeyError, TypeError, AttributeError):
                return False
            confirmed.append((runtime, binding, disks[0], reference))
        for runtime, binding, disk, reference in confirmed:
            disk.provider_ref = reference
            disk.state = "PRESENT"
            disk.observed_at = observation.sample_finished_at
            binding.observed_at = observation.sample_finished_at
            runtime.state = "STOPPED"
            runtime.observed_at = observation.sample_finished_at
        for member in run_roster:
            member.state = "READY"
        run.state = "READY"
        return True


async def process(sessions) -> bool:
    """Observe committed node receipts before advancing a lesson roster."""
    async with sessions() as db:
        candidates = list(
            await db.execute(
                select(EnvironmentRun.id, Operation.id)
                .join(Operation, Operation.environment_id == EnvironmentRun.environment_id)
                .where(
                    EnvironmentRun.state == "PREPARING",
                    Operation.kind == "LESSON_START",
                    Operation.state == "WAITING_NODE",
                )
                .order_by(EnvironmentRun.created_at, EnvironmentRun.id)
                .limit(10)
            )
        )
    for run_id, operation_id in candidates:
        if await reconcile_created_run(sessions, run_id=run_id, operation_id=operation_id):
            return True
    return False
