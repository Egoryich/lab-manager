"""Stable network claims for guests that persist between lessons."""

import uuid
from collections import defaultdict

from lab_manager.network_allocations import claim_network_segment


def segment_key(environment_id: uuid.UUID, runtime_id: uuid.UUID, mode: str, internet: bool):
    if mode == "ISOLATED":
        return runtime_id
    if mode == "GROUP_LAN":
        return uuid.uuid5(environment_id, f"group-lan:internet={int(internet)}")
    raise ValueError("Invalid network mode")


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
        for runtime in members:
            allocations[runtime.id] = allocation.id
    return allocations
