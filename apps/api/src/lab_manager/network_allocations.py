"""Serialized guest subnet claims; applying and releasing remain separate operations."""

import uuid
from ipaddress import IPv4Network

from sqlalchemy import select, text

from lab_manager.catalog_models import Environment
from lab_manager.ipam import first_available_subnet, parse_private_pool, prefix_for_usable_hosts
from lab_manager.network_models import NetworkSegmentAllocation, NodeNetworkPool


class NetworkAllocationRejected(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


async def reserve_network_segment(
    sessions,
    *,
    node_id: uuid.UUID,
    environment_id: uuid.UUID,
    teacher_id: uuid.UUID,
    segment_key: uuid.UUID,
    mode: str,
    requested_hosts: int,
) -> NetworkSegmentAllocation:
    """Claim a subnet atomically, without connecting any guest or allowing traffic.

    All claims for a node lock the pool row before reading existing allocations.
    Repeating the same key within its reserved host capacity returns the
    original claim. A released claim is never silently resurrected.
    """
    async with sessions() as db, db.begin():
        await db.execute(text("SET LOCAL statement_timeout = '10s'"))
        await db.execute(text("SET LOCAL lock_timeout = '5s'"))
        environment = await db.get(Environment, environment_id)
        if environment is None or environment.owner_teacher_id != teacher_id:
            raise NetworkAllocationRejected("ENVIRONMENT_NOT_OWNED")
        return await claim_network_segment(
            db,
            node_id=node_id,
            environment_id=environment_id,
            segment_key=segment_key,
            mode=mode,
            requested_hosts=requested_hosts,
        )


async def claim_network_segment(
    db,
    *,
    node_id: uuid.UUID,
    environment_id: uuid.UUID,
    segment_key: uuid.UUID,
    mode: str,
    requested_hosts: int,
) -> NetworkSegmentAllocation:
    """Claim inside the caller's transaction, after environment ownership validation."""
    if mode not in ("ISOLATED", "GROUP_LAN"):
        raise NetworkAllocationRejected("INVALID_MODE")
    try:
        prefix = prefix_for_usable_hosts(requested_hosts)
    except ValueError as error:
        raise NetworkAllocationRejected("INVALID_HOST_COUNT") from error
    pool = await db.scalar(
        select(NodeNetworkPool).where(NodeNetworkPool.node_id == node_id).with_for_update()
    )
    if pool is None:
        raise NetworkAllocationRejected("NETWORK_POOL_MISSING")
    existing = await db.scalar(
        select(NetworkSegmentAllocation).where(
            NetworkSegmentAllocation.environment_id == environment_id,
            NetworkSegmentAllocation.segment_key == segment_key,
        )
    )
    if existing is not None:
        if (
            existing.node_id == node_id
            and existing.mode == mode
            and existing.requested_hosts >= requested_hosts
            and existing.state != "RELEASED"
        ):
            return existing
        raise NetworkAllocationRejected("SEGMENT_KEY_CONFLICT")
    try:
        cidr = parse_private_pool(pool.cidr)
    except ValueError as error:
        raise NetworkAllocationRejected("NETWORK_POOL_INVALID") from error
    if prefix < cidr.prefixlen:
        raise NetworkAllocationRejected("NETWORK_POOL_TOO_SMALL")
    claimed = list(
        await db.scalars(
            select(NetworkSegmentAllocation.cidr).where(
                NetworkSegmentAllocation.node_id == node_id,
                NetworkSegmentAllocation.state != "RELEASED",
            )
        )
    )
    try:
        occupied = [IPv4Network(value, strict=True) for value in claimed]
    except ValueError as error:
        raise NetworkAllocationRejected("NETWORK_ALLOCATIONS_INVALID") from error
    subnet = first_available_subnet(cidr, prefix, occupied)
    if subnet is None:
        raise NetworkAllocationRejected("NETWORK_POOL_EXHAUSTED")
    allocation = NetworkSegmentAllocation(
        node_id=node_id,
        environment_id=environment_id,
        segment_key=segment_key,
        mode=mode,
        requested_hosts=(1 << (32 - prefix)) - 2,
        cidr=str(subnet),
        state="RESERVED",
    )
    db.add(allocation)
    await db.flush()
    return allocation
