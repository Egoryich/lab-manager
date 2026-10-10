import uuid
from types import SimpleNamespace

from lab_manager.lesson_lxc_start_reconciliation import observed_running


def test_start_receipt_only_counts_after_matching_owned_running_guest():
    runtime_id, node_id = uuid.uuid4(), uuid.uuid4()
    marker = f"lab-manager:runtime={runtime_id};generation=1"
    runtime = SimpleNamespace(
        id=runtime_id, node_id=node_id, generation=1, kind="LXC", state="STARTING"
    )
    binding = SimpleNamespace(
        runtime_id=runtime_id,
        node_id=node_id,
        generation=1,
        vmid=901100,
        ownership_marker=marker,
    )
    command = SimpleNamespace(
        kind="LXC_START",
        state="SUCCEEDED",
        runtime_id=runtime_id,
        node_id=node_id,
        vmid=901100,
        generation=1,
    )
    guest = {
        "vmid": 901100,
        "kind": "LXC",
        "reported_status": "running",
        "ownership_marker": marker,
    }
    sample = {"ownership_reconciled": True, "guests": [guest]}
    assert observed_running(command, runtime, binding, sample)
    assert not observed_running(command, runtime, binding, {**sample, "guests": []})
    assert not observed_running(command, runtime, binding, {**sample, "guests": [guest, guest]})
    assert not observed_running(
        command, runtime, binding, {**sample, "guests": [{**guest, "reported_status": "stopped"}]}
    )
    assert not observed_running(
        command, runtime, binding, {**sample, "ownership_reconciled": False}
    )
