"""Stable network claims for guests that persist between lessons."""

import uuid
from collections import defaultdict
from ipaddress import IPv4Address, IPv4Network

from sqlalchemy import select

from lab_manager.network_allocations import NetworkAllocationRejected, claim_network_segment
from lab_manager.runtime_models import Runtime


def segment_key(environment_id: uuid.UUID, runtime_id: uuid.UUID, mode: str, internet: bool):
    if mode == "ISOLATED":
        return runtime_id
    if mode == "GROUP_LAN":
        return uuid.uuid5(environment_id, f"group-lan:internet={int(internet)}")
    raise ValueError("Invalid network mode")


def assign_member_addresses(members, previous, allocation, *, environment_id, node_id):
    """Keep existing guest addresses and reserve the first host for the gateway."""
    try:
        subnet = IPv4Network(allocation.cidr, strict=True)
        gateway = subnet.network_address + 1
    except ValueError as error:
        raise NetworkAllocationRejected("SEGMENT_CIDR_INVALID") from error
    used = set()
    for runtime in previous:
        try:
            address = IPv4Address(runtime.guest_ipv4)
        except (TypeError, ValueError) as error:
            raise NetworkAllocationRejected("GUEST_ADDRESS_INVALID") from error
        if (
            runtime.environment_id != environment_id
            or runtime.node_id != node_id
            or address not in subnet
            or address in (subnet.network_address, subnet.broadcast_address, gateway)
            or address in used
        ):
            raise NetworkAllocationRejected("GUEST_ADDRESS_INVALID")
        used.add(address)
    available = (
        address for address in subnet.hosts() if address != gateway and address not in used
    )
    for runtime in sorted(members, key=lambda member: member.id):
        if runtime.network_allocation_id is None and runtime.guest_ipv4 is None:
            address = next(available, None)
            if address is None:
                raise NetworkAllocationRejected("SEGMENT_ADDRESS_EXHAUSTED")
            runtime.network_allocation_id = allocation.id
            runtime.guest_ipv4 = str(address)
            used.add(address)
        elif runtime.network_allocation_id != allocation.id or not runtime.guest_ipv4:
            raise NetworkAllocationRejected("GUEST_ADDRESS_CHANGED")


async def claim_run_networks(db, *, environment_id, node_id, runtimes, profiles):
    """Reserve all required subnets within the run-preparation transaction.

    An isolated guest gets its own /30. Shared guests with the same Internet
    policy share one subnet. A changed roster that no longer fits the original
    claim fails closed; changing a live subnet needs a separate migration.
    """
    grouped = defaultdict(list)
    for runtime in runtimes:
        profile = profiles[runtime.profile_version_id]
        key = segment_key(
            environment_id, runtime.id, profile.network_mode, profile.internet_enabled
        )
        grouped[(key, profile.network_mode)].append(runtime)
    allocations = {}
    for (key, mode), members in sorted(grouped.items(), key=lambda item: str(item[0][0])):
        allocation = await claim_network_segment(
            db,
            node_id=node_id,
            environment_id=environment_id,
            segment_key=key,
            mode=mode,
            requested_hosts=max(2, len(members) + 1),
        )
        previous = list(
            await db.scalars(select(Runtime).where(Runtime.network_allocation_id == allocation.id))
        )
        assign_member_addresses(
            members, previous, allocation, environment_id=environment_id, node_id=node_id
        )
        for runtime in members:
            allocations[runtime.id] = allocation.id
    return allocations
