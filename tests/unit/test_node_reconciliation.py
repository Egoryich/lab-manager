from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from lab_manager.node_reconciliation import reconcile


def sample():
    now = datetime.now(UTC)
    return {
        "sample_finished_at": now.isoformat(),
        "local_thin_sample_finished_at": (now - timedelta(seconds=5)).isoformat(),
        "guests": [],
        "storages": [{"name": "student-lvm", "backend": "lvmthin"}],
        "local_thin_pools": [
            {
                "storage": "student-lvm",
                "volumes": [],
            }
        ],
        "limitations": ["DISK_COMMITMENTS_NOT_RECONCILED"],
        "admission_ready": False,
    }


def test_empty_node_reconciles_without_opening_admission():
    result = reconcile(sample(), [], [])
    assert result["ownership_reconciled"] is True
    assert result["storages"][0]["commitments_reconciled"] is True
    assert result["storages"][0]["external_committed_bytes"] == 0
    assert result["admission_ready"] is False


def test_guest_marker_requires_exact_database_binding():
    data = sample()
    runtime_id = uuid4()
    marker = f"lab-manager:runtime={runtime_id};generation=1"
    data["guests"] = [
        {
            "vmid": 301,
            "ownership_marker": marker,
            "reported_status": "stopped",
        }
    ]
    assert reconcile(data, [], [])["ownership_reconciled"] is False
    binding = SimpleNamespace(
        vmid=301, runtime_id=runtime_id, generation=1, ownership_marker=marker
    )
    assert reconcile(data, [binding], [])["ownership_reconciled"] is True
    binding.ownership_marker = marker.replace("generation=1", "generation=2")
    assert reconcile(data, [binding], [])["ownership_reconciled"] is False


def test_external_guest_and_volume_are_accounted_conservatively():
    data = sample()
    data["guests"] = [
        {
            "vmid": 300,
            "ownership_marker": None,
            "reported_status": "running",
            "memory_limit_bytes": 513 * 2**20,
            "reported_vcpus": 2,
        }
    ]
    data["local_thin_pools"][0]["volumes"] = [{"name": "vm-300-disk-0", "size_bytes": 4 * 2**30}]
    result = reconcile(data, [], [])
    assert result["ownership_reconciled"] is True
    assert result["external_running_memory_mib"] == 513
    assert result["external_cpu_millicredits"] == 2000
    assert result["storages"][0]["external_committed_bytes"] == 4 * 2**30


def test_known_disk_excluded_from_external_and_missing_disk_closes_reconciliation():
    data = sample()
    data["local_thin_pools"][0]["volumes"] = [{"name": "vm-301-disk-0", "size_bytes": 4 * 2**30}]
    disk = SimpleNamespace(
        storage_name="student-lvm",
        state="PRESENT",
        provider_ref="student-lvm:vm-301-disk-0",
        logical_bytes=4 * 2**30,
    )
    assert reconcile(data, [], [disk])["storages"][0]["external_committed_bytes"] == 0
    disk.provider_ref = "student-lvm:vm-302-disk-0"
    assert reconcile(data, [], [disk])["storages"][0]["commitments_reconciled"] is False
    data["local_thin_sample_finished_at"] = (datetime.now(UTC) - timedelta(minutes=3)).isoformat()
    assert reconcile(data, [], [])["storages"][0]["commitments_reconciled"] is False
