"""Deterministic IPv4 subnet selection; the caller must lock the pool in PostgreSQL."""

from ipaddress import IPv4Network, ip_network

PRIVATE_POOLS = tuple(
    IPv4Network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


def parse_private_pool(value: str) -> IPv4Network:
    """Require an explicit, aligned RFC1918 pool for student networks."""
    try:
        network = ip_network(value, strict=True)
    except ValueError as error:
        raise ValueError("Invalid IPv4 network pool") from error
    if not isinstance(network, IPv4Network) or not any(
        network.subnet_of(private) for private in PRIVATE_POOLS
    ):
        raise ValueError("Network pool must be within RFC1918 space")
    return network


def prefix_for_usable_hosts(hosts: int) -> int:
    """Smallest conventional IPv4 subnet for guests plus their gateway."""
    if type(hosts) is not int or not 2 <= hosts <= 65534:
        raise ValueError("Invalid host count")
    host_bits = max(2, (hosts + 2 - 1).bit_length())
    return 32 - host_bits


def first_available_subnet(
    pool: IPv4Network, prefix: int, occupied: list[IPv4Network]
) -> IPv4Network | None:
    """Find the lowest free aligned subnet without enumerating the whole pool.

    `occupied` includes reservations still awaiting confirmed detach and excluded
    infrastructure ranges. Database callers serialize this selection with a
    SELECT FOR UPDATE lock on the pool row before inserting the allocation.
    """
    if not isinstance(pool, IPv4Network) or not any(
        pool.subnet_of(private) for private in PRIVATE_POOLS
    ):
        raise ValueError("Invalid private IPv4 pool")
    if type(prefix) is not int or not pool.prefixlen <= prefix <= 30:
        raise ValueError("Invalid subnet prefix")
    if any(not isinstance(item, IPv4Network) for item in occupied):
        raise ValueError("Occupied ranges must be IPv4 networks")

    size = 1 << (32 - prefix)
    end = int(pool.broadcast_address) + 1
    cursor = int(pool.network_address)
    ranges = sorted(
        (
            max(cursor, int(item.network_address)),
            min(end, int(item.broadcast_address) + 1),
        )
        for item in occupied
        if item.overlaps(pool)
    )
    for blocked_start, blocked_end in ranges:
        candidate = (cursor + size - 1) // size * size
        if candidate + size <= blocked_start:
            return IPv4Network((candidate, prefix))
        cursor = max(cursor, blocked_end)
    candidate = (cursor + size - 1) // size * size
    if candidate + size <= end:
        return IPv4Network((candidate, prefix))
    return None
