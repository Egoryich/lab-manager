"""Apply reserved lesson bridges through the pinned node transport.

The node operation is idempotent. If its response is lost, the next attempt
checks the node's persisted allocation before repeating the create request.
"""

import asyncio
import logging

from sqlalchemy import exists, select, text

from lab_manager.catalog_models import ProfileVersion
from lab_manager.lesson_network import segment_key
from lab_manager.network_models import NetworkSegmentAllocation
from lab_manager.node_segments import NodeSegmentClient, NodeSegmentError
from lab_manager.runtime_models import EnvironmentRun, RunRuntime, Runtime

logger = logging.getLogger("lab_manager.node_segments")


async def process(sessions, endpoints, *, client_factory=NodeSegmentClient) -> bool:
    if not endpoints:
        return False
    endpoint_by_id = {endpoint.id: endpoint for endpoint in endpoints}
    async with sessions() as db, db.begin():
        await db.execute(text("SET LOCAL statement_timeout = '10s'"))
        await db.execute(text("SET LOCAL lock_timeout = '3s'"))
        pending = exists(
            select(NetworkSegmentAllocation.id).where(
                NetworkSegmentAllocation.environment_id == EnvironmentRun.environment_id,
                NetworkSegmentAllocation.state == "RESERVED",
            )
        )
        runs = list(
            await db.scalars(
                select(EnvironmentRun)
                .where(
                    EnvironmentRun.state == "PREPARING",
                    EnvironmentRun.node_id.in_(endpoint_by_id),
                    pending,
                )
                .order_by(EnvironmentRun.created_at, EnvironmentRun.id)
                .with_for_update(skip_locked=True)
                .limit(10)
            )
        )
        for run in runs:
            guests = list(
                await db.execute(
                    select(Runtime, ProfileVersion)
                    .join(RunRuntime, RunRuntime.runtime_id == Runtime.id)
                    .join(ProfileVersion, ProfileVersion.id == Runtime.profile_version_id)
                    .where(RunRuntime.run_id == run.id)
                )
            )
            keys = {
                segment_key(
                    run.environment_id,
                    runtime.id,
                    profile.network_mode,
                    profile.internet_enabled,
                )
                for runtime, profile in guests
            }
            if not keys:
                continue
            allocation = await db.scalar(
                select(NetworkSegmentAllocation)
                .where(
                    NetworkSegmentAllocation.environment_id == run.environment_id,
                    NetworkSegmentAllocation.node_id == run.node_id,
                    NetworkSegmentAllocation.segment_key.in_(keys),
                    NetworkSegmentAllocation.state == "RESERVED",
                )
                .order_by(NetworkSegmentAllocation.created_at, NetworkSegmentAllocation.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if allocation is None:
                continue
            client = client_factory(endpoint_by_id[run.node_id])
            try:
                async with asyncio.timeout(25):
                    observed = await asyncio.to_thread(client.status, allocation.id)
                    if observed is None:
                        observed = await asyncio.to_thread(
                            client.create, allocation.id, allocation.mode, allocation.cidr
                        )
                if observed["mode"] != allocation.mode or observed["cidr"] != allocation.cidr:
                    raise NodeSegmentError("SEGMENT_RESPONSE_MISMATCH")
            except (NodeSegmentError, TimeoutError) as error:
                logger.warning(
                    "segment_apply_failed allocation=%s code=%s",
                    allocation.id,
                    getattr(error, "code", "NODE_SEGMENT_TIMEOUT"),
                )
                return False
            allocation.state = "APPLIED"
            return True
    return False
