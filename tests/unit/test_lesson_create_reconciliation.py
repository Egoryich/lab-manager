import uuid
from types import SimpleNamespace

import pytest
from lab_manager.lesson_create_reconciliation import CreateReconciliationError, disk_reference


def fixture():
    runtime_id, node_id = uuid.uuid4(), uuid.uuid4()
    marker = f"lab-manager:runtime={runtime_id};generation=1"
    command = SimpleNamespace(
        state="SUCCEEDED",
        kind="LXC_CREATE",
        runtime_id=runtime_id,
        node_id=node_id,
        vmid=900001,
        generation=1,
    )
    runtime = SimpleNamespace(
        id=runtime_id,
        node_id=node_id,
        state="PROVISIONING",
        generation=1,
    )
    binding = SimpleNamespace(
        runtime_id=runtime_id,
        node_id=node_id,
        vmid=900001,
        ownership_marker=marker,
    )
    disk = SimpleNamespace(
        runtime_id=runtime_id,
        node_id=node_id,
        storage_name="student-lvm",
        state="PLANNED",
        provider_ref=None,
        logical_bytes=4 * 1024**3,
    )
    sample = {
        "ownership_reconciled": True,
        "guests": [
            {
                "vmid": 900001,
                "kind": "LXC",
                "reported_status": "stopped",
                "ownership_marker": marker,
            }
        ],
        "local_thin_pools": [
            {
                "storage": "student-lvm",
                "volumes": [{"name": "vm-900001-disk-0", "size_bytes": 4 * 1024**3}],
            }
        ],
    }
    return command, runtime, binding, disk, sample


def test_created_disk_requires_exact_owned_guest_and_single_full_size_volume():
    values = fixture()
    assert disk_reference(*values) == "student-lvm:vm-900001-disk-0"
    values[-1]["guests"][0]["ownership_marker"] = None
    with pytest.raises(CreateReconciliationError):
        disk_reference(*values)
    values = fixture()
    values[-1]["local_thin_pools"][0]["volumes"].append(
        {"name": "vm-900001-disk-1", "size_bytes": 4 * 1024**3}
    )
    with pytest.raises(CreateReconciliationError):
        disk_reference(*values)


def test_submitted_task_or_wrong_size_does_not_materialize_disk():
    values = fixture()
    values[0].state = "SUBMITTED"
    with pytest.raises(CreateReconciliationError):
        disk_reference(*values)
    values = fixture()
    values[-1]["local_thin_pools"][0]["volumes"][0]["size_bytes"] -= 1
    with pytest.raises(CreateReconciliationError):
        disk_reference(*values)
