"""Prepare an already allocated lesson gateway while guest links remain closed."""

import asyncio
import hashlib
import logging

from sqlalchemy import func, select

from lab_manager.network_models import NetworkSegmentAllocation
from lab_manager.node_segments import NodeSegmentClient, NodeSegmentError
from lab_manager.operation_models import Operation
from lab_manager.reservation_models import LessonReservation
from lab_manager.runtime_models import EnvironmentRun, RunRuntime, Runtime

logger = logging.getLogger("lab_manager.lesson_gateway")


async def process(sessions, endpoints, *, client_factory=NodeSegmentClient) -> bool:
    """Apply one missing gateway; a lost response is resolved by reading node state."""
    if not endpoints:
        return False
    endpoint_by_id = {endpoint.id: endpoint for endpoint in endpoints}
    async with sessions() as db:
        candidates = list(
            await db.execute(
                select(Operation, EnvironmentRun)
                .select_from(EnvironmentRun)
                .join(LessonReservation, LessonReservation.id == EnvironmentRun.reservation_id)
                .join(Operation, Operation.environment_id == EnvironmentRun.environment_id)
                .where(
                    EnvironmentRun.state == "READY",
                    EnvironmentRun.node_id.in_(endpoint_by_id),
                    LessonReservation.state == "ACTIVE",
                    LessonReservation.ends_at > func.clock_timestamp(),
                    Operation.kind == "LESSON_START",
                    Operation.state == "WAITING_NODE",
                )
                .order_by(EnvironmentRun.created_at, EnvironmentRun.id)
                .limit(10)
            )
        )
        for operation, run in candidates:
            if (
                operation.request_digest
                != hashlib.sha256(f"LESSON_START:{run.reservation_id}".encode()).hexdigest()
            ):
                continue
            roster = list(
                await db.execute(
                    select(Runtime.network_allocation_id, NetworkSegmentAllocation)
                    .join(RunRuntime, RunRuntime.runtime_id == Runtime.id)
                    .outerjoin(
                        NetworkSegmentAllocation,
                        NetworkSegmentAllocation.id == Runtime.network_allocation_id,
                    )
                    .where(RunRuntime.run_id == run.id)
                )
            )
            if not roster or any(
                allocation_id is None
                or item is None
                or item.node_id != run.node_id
                or item.environment_id != run.environment_id
                or item.state != "APPLIED"
                for allocation_id, item in roster
            ):
                continue
            allocations = {item.id: item for _, item in roster}
            client = client_factory(endpoint_by_id[run.node_id])
            for allocation in sorted(allocations.values(), key=lambda item: item.id):
                try:
                    async with asyncio.timeout(25):
                        observed = await asyncio.to_thread(client.status, allocation.id)
                        if observed is None:
                            raise NodeSegmentError("SEGMENT_NOT_FOUND")
                        if (
                            observed["mode"] != allocation.mode
                            or observed["cidr"] != allocation.cidr
                        ):
                            raise NodeSegmentError("SEGMENT_RESPONSE_MISMATCH")
                        if observed["state"] == "CREATED":
                            await asyncio.to_thread(
                                client.prepare_gateway,
                                allocation.id,
                                allocation.mode,
                                allocation.cidr,
                            )
                            return True
                except (NodeSegmentError, TimeoutError) as error:
                    logger.warning(
                        "lesson_gateway_failed allocation=%s code=%s",
                        allocation.id,
                        getattr(error, "code", "NODE_SEGMENT_TIMEOUT"),
                    )
                    return False
    return False
