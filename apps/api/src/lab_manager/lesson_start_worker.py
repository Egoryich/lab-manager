"""Turn a prepared lesson with applied bridges into durable create commands."""

import hashlib
import logging

from sqlalchemy import exists, select

from lab_manager.lesson_command_queue import queue_lxc_create_commands
from lab_manager.lesson_commands import LessonCommandRejected
from lab_manager.network_models import NetworkSegmentAllocation
from lab_manager.node_command_models import NodeCommand
from lab_manager.operation_models import Operation
from lab_manager.runtime_models import EnvironmentRun, RunRuntime, Runtime

logger = logging.getLogger("lab_manager.lesson_start")


async def process(sessions, codec) -> bool:
    """Queue one lesson at a time; a failed transaction never exposes a partial roster."""
    async with sessions() as db:
        commands_exist = exists(
            select(NodeCommand.id).where(NodeCommand.parent_operation_id == Operation.id)
        )
        candidate = await db.execute(
            select(Operation, EnvironmentRun)
            .join(EnvironmentRun, EnvironmentRun.environment_id == Operation.environment_id)
            .where(
                Operation.kind == "LESSON_START",
                Operation.state == "WAITING_NODE",
                EnvironmentRun.state == "PREPARING",
                ~commands_exist,
            )
            .order_by(Operation.created_at, Operation.id)
            .limit(1)
        )
        pair = candidate.first()
        if pair is None:
            return False
        operation, run = pair
        if (
            operation.request_digest
            != hashlib.sha256(f"LESSON_START:{run.reservation_id}".encode()).hexdigest()
        ):
            logger.error("lesson_start_identity_mismatch operation=%s", operation.id)
            return False
        dependencies = list(
            await db.execute(
                select(Runtime.network_allocation_id, NetworkSegmentAllocation.state)
                .join(RunRuntime, RunRuntime.runtime_id == Runtime.id)
                .outerjoin(
                    NetworkSegmentAllocation,
                    NetworkSegmentAllocation.id == Runtime.network_allocation_id,
                )
                .where(RunRuntime.run_id == run.id)
            )
        )
        if not dependencies or any(
            allocation_id is None or state != "APPLIED" for allocation_id, state in dependencies
        ):
            return False
        operation_id, run_id = operation.id, run.id
    try:
        await queue_lxc_create_commands(
            sessions, run_id=run_id, operation_id=operation_id, codec=codec
        )
    except LessonCommandRejected as error:
        logger.warning("lesson_start_not_ready operation=%s code=%s", operation_id, error.code)
        return False
    return True
