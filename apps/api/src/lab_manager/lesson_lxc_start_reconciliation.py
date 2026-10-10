"""Confirm LXC starts from a fresh owned Proxmox observation, not a command receipt."""

import uuid

from sqlalchemy import func, select, text

from lab_manager.node_command_models import NodeCommand
from lab_manager.nodes import NodeObservation
from lab_manager.operation_models import Operation
from lab_manager.runtime_models import EnvironmentRun, ProviderRuntimeBinding, RunRuntime, Runtime


def observed_running(command, runtime, binding, sample: dict) -> bool:
    marker = f"lab-manager:runtime={runtime.id};generation={runtime.generation}"
    if (
        command.kind != "LXC_START"
        or command.state != "SUCCEEDED"
        or command.runtime_id != runtime.id
        or command.node_id != runtime.node_id
        or command.vmid != binding.vmid
        or command.generation != runtime.generation
        or runtime.state != "STARTING"
        or runtime.kind != "LXC"
        or binding.runtime_id != runtime.id
        or binding.node_id != runtime.node_id
        or binding.generation != runtime.generation
        or binding.ownership_marker != marker
        or not isinstance(sample, dict)
        or sample.get("ownership_reconciled") is not True
        or not isinstance(sample.get("guests"), list)
    ):
        return False
    guests = [
        item
        for item in sample["guests"]
        if isinstance(item, dict) and item.get("vmid") == binding.vmid
    ]
    return len(guests) == 1 and all(
        guests[0].get(key) == value
        for key, value in (
            ("kind", "LXC"),
            ("reported_status", "running"),
            ("ownership_marker", marker),
        )
    )


async def reconcile_started_run(sessions, *, run_id: uuid.UUID, operation_id: uuid.UUID) -> bool:
    """Advance guest state together; the lesson stays READY until SSH is admitted."""
    async with sessions() as db, db.begin():
        await db.execute(text("SET LOCAL statement_timeout = '10s'"))
        await db.execute(text("SET LOCAL lock_timeout = '5s'"))
        run = await db.scalar(
            select(EnvironmentRun).where(EnvironmentRun.id == run_id).with_for_update()
        )
        operation = await db.get(Operation, operation_id)
        if (
            run is None
            or run.state != "READY"
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
                select(NodeCommand).where(
                    NodeCommand.parent_operation_id == operation_id,
                    NodeCommand.kind == "LXC_START",
                )
            )
        )
        runtimes = list(
            await db.scalars(
                select(Runtime)
                .join(RunRuntime, RunRuntime.runtime_id == Runtime.id)
                .where(RunRuntime.run_id == run.id)
                .order_by(Runtime.id)
                .with_for_update(of=Runtime)
            )
        )
        roster = list(await db.scalars(select(RunRuntime).where(RunRuntime.run_id == run.id)))
        if (
            not runtimes
            or len(roster) != len(runtimes)
            or len(commands) != len(runtimes)
            or {command.runtime_id for command in commands} != {runtime.id for runtime in runtimes}
            or any(command.finished_at is None for command in commands)
            or any(observation.last_contact_at < command.finished_at for command in commands)
            or any(member.state != "READY" for member in roster)
        ):
            return False
        by_runtime = {item.runtime_id: item for item in commands}
        for runtime in runtimes:
            binding = await db.get(ProviderRuntimeBinding, runtime.id)
            if binding is None or not observed_running(
                by_runtime[runtime.id], runtime, binding, observation.payload["sample"]
            ):
                return False
        for runtime in runtimes:
            runtime.state = "RUNNING"
            runtime.observed_at = observation.sample_finished_at
        return True


async def process(sessions) -> bool:
    async with sessions() as db:
        candidates = list(
            await db.execute(
                select(EnvironmentRun.id, Operation.id)
                .select_from(EnvironmentRun)
                .join(Operation, Operation.environment_id == EnvironmentRun.environment_id)
                .where(
                    EnvironmentRun.state == "READY",
                    Operation.kind == "LESSON_START",
                    Operation.state == "WAITING_NODE",
                )
                .order_by(EnvironmentRun.created_at, EnvironmentRun.id)
                .limit(10)
            )
        )
    for run_id, operation_id in candidates:
        if await reconcile_started_run(sessions, run_id=run_id, operation_id=operation_id):
            return True
    return False
