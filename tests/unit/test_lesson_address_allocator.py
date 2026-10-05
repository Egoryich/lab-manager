import uuid
from types import SimpleNamespace

import pytest
from lab_manager.lesson_network import assign_member_addresses
from lab_manager.network_allocations import NetworkAllocationRejected


def member(environment_id, node_id):
    return SimpleNamespace(
        id=uuid.uuid4(),
        environment_id=environment_id,
        node_id=node_id,
        network_allocation_id=None,
        guest_ipv4=None,
    )


def test_addresses_survive_repeated_lesson_and_added_member():
    environment_id, node_id = uuid.uuid4(), uuid.uuid4()
    allocation = SimpleNamespace(id=uuid.uuid4(), cidr="10.70.1.0/29")
    first, second = member(environment_id, node_id), member(environment_id, node_id)
    assign_member_addresses(
        [second, first], [], allocation, environment_id=environment_id, node_id=node_id
    )
    assert {first.guest_ipv4, second.guest_ipv4} == {"10.70.1.2", "10.70.1.3"}
    original = {first.id: first.guest_ipv4, second.id: second.guest_ipv4}
    third = member(environment_id, node_id)
    assign_member_addresses(
        [third, second, first],
        [second, first],
        allocation,
        environment_id=environment_id,
        node_id=node_id,
    )
    assert {first.id: first.guest_ipv4, second.id: second.guest_ipv4} == original
    assert third.guest_ipv4 == "10.70.1.4"


def test_no_guest_can_take_gateway_or_reuse_an_address():
    environment_id, node_id = uuid.uuid4(), uuid.uuid4()
    allocation = SimpleNamespace(id=uuid.uuid4(), cidr="10.70.1.0/29")
    first, second = member(environment_id, node_id), member(environment_id, node_id)
    first.network_allocation_id = second.network_allocation_id = allocation.id
    first.guest_ipv4 = second.guest_ipv4 = "10.70.1.2"
    with pytest.raises(NetworkAllocationRejected, match="GUEST_ADDRESS_INVALID"):
        assign_member_addresses(
            [first, second],
            [first, second],
            allocation,
            environment_id=environment_id,
            node_id=node_id,
        )
    first.guest_ipv4 = "10.70.1.1"
    with pytest.raises(NetworkAllocationRejected, match="GUEST_ADDRESS_INVALID"):
        assign_member_addresses(
            [first], [first], allocation, environment_id=environment_id, node_id=node_id
        )
