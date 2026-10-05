import uuid
from types import SimpleNamespace

import pytest
from lab_manager.lesson_commands import LessonCommandRejected, create_lxc_payload


def identities():
    node_id, environment_id, runtime_id, allocation_id = (uuid.uuid4() for _ in range(4))
    runtime = SimpleNamespace(
        id=runtime_id,
        node_id=node_id,
        environment_id=environment_id,
        network_allocation_id=allocation_id,
        guest_ipv4="10.70.1.2",
        generation=1,
        kind="LXC",
        state="PLANNED",
        memory_mib=512,
        vcpu=1,
        disk_gib=4,
    )
    binding = SimpleNamespace(
        runtime_id=runtime_id,
        node_id=node_id,
        generation=1,
        ownership_marker=f"lab-manager:runtime={runtime_id};generation=1",
        vmid=900001,
    )
    allocation = SimpleNamespace(
        id=allocation_id,
        node_id=node_id,
        environment_id=environment_id,
        cidr="10.70.1.0/30",
        mode="ISOLATED",
        state="APPLIED",
    )
    template = SimpleNamespace(
        runtime_kind="LXC",
        source_ref="local:vztmpl/debian-13-standard_13.6-1_amd64.tar.zst",
    )
    return runtime, binding, allocation, template


def test_payload_matches_exact_node_command_contract():
    runtime, binding, allocation, template = identities()
    operation_id = uuid.uuid4()
    payload = create_lxc_payload(
        operation_id=operation_id,
        runtime=runtime,
        binding=binding,
        allocation=allocation,
        template=template,
        storage_name="student-lvm",
        ssh_public_key="ssh-ed25519 " + "A" * 68,
    )
    assert payload["operation_id"] == str(operation_id)
    assert payload["vmid"] == 900001
    assert payload["spec"]["template"] == template.source_ref
    assert payload["spec"]["address"] == "10.70.1.2"
    assert payload["spec"]["segment"] == {
        "allocation_id": str(allocation.id),
        "mode": "ISOLATED",
        "cidr": "10.70.1.0/30",
    }


def test_no_create_command_for_unapplied_or_mismatched_segment():
    runtime, binding, allocation, template = identities()
    allocation.state = "RESERVED"
    values = dict(
        operation_id=uuid.uuid4(),
        runtime=runtime,
        binding=binding,
        allocation=allocation,
        template=template,
        storage_name="student-lvm",
        ssh_public_key="ssh-ed25519 " + "A" * 68,
    )
    with pytest.raises(LessonCommandRejected):
        create_lxc_payload(**values)
    allocation.state = "APPLIED"
    runtime.guest_ipv4 = "10.70.1.1"
    with pytest.raises(LessonCommandRejected):
        create_lxc_payload(**values)
    runtime.guest_ipv4 = "10.70.1.2"
    template.source_ref = None
    with pytest.raises(LessonCommandRejected):
        create_lxc_payload(**values)
