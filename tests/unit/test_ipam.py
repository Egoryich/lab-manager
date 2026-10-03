from ipaddress import IPv4Network, IPv6Network

import pytest
from lab_manager.ipam import first_available_subnet, parse_private_pool, prefix_for_usable_hosts


def test_one_runtime_and_group_sizes_include_gateway_and_demo():
    assert prefix_for_usable_hosts(2) == 30  # one student plus gateway
    assert prefix_for_usable_hosts(32) == 26  # 30 students, demo and gateway
    assert prefix_for_usable_hosts(33) == 26
    assert prefix_for_usable_hosts(63) == 25


def test_pool_must_be_aligned_ipv4_and_inside_private_space():
    assert parse_private_pool("10.70.0.0/16") == IPv4Network("10.70.0.0/16")
    for value in ("10.70.1.0/16", "198.18.0.0/16", "fd00::/64", "not a network"):
        with pytest.raises(ValueError):
            parse_private_pool(value)


def test_allocates_first_free_segment_regardless_of_occupied_order():
    pool = parse_private_pool("10.70.0.0/24")
    occupied = [IPv4Network("10.70.0.8/30"), IPv4Network("10.70.0.0/30")]
    assert first_available_subnet(pool, 30, occupied) == IPv4Network("10.70.0.4/30")
    assert first_available_subnet(pool, 30, list(reversed(occupied))) == IPv4Network("10.70.0.4/30")


def test_partially_overlapping_exclusion_and_exhaustion():
    pool = parse_private_pool("10.70.0.0/29")
    assert first_available_subnet(pool, 30, [IPv4Network("10.70.0.0/30")]) == IPv4Network(
        "10.70.0.4/30"
    )
    assert (
        first_available_subnet(pool, 30, [IPv4Network("10.70.0.0/30"), IPv4Network("10.70.0.4/30")])
        is None
    )
    assert first_available_subnet(pool, 30, [IPv4Network("10.70.0.0/28")]) is None


def test_large_pool_gap_selection_does_not_scan_every_subnet():
    pool = parse_private_pool("10.0.0.0/8")
    assert first_available_subnet(pool, 30, [IPv4Network("10.0.0.0/9")]) == IPv4Network(
        "10.128.0.0/30"
    )


def test_invalid_counts_prefixes_and_address_families_are_rejected():
    for count in (0, 1, 65535, True):
        with pytest.raises(ValueError):
            prefix_for_usable_hosts(count)
    pool = parse_private_pool("10.70.0.0/24")
    for prefix in (23, 31, True):
        with pytest.raises(ValueError):
            first_available_subnet(pool, prefix, [])
    with pytest.raises(ValueError):
        first_available_subnet(pool, 30, [IPv6Network("fd00::/64")])
