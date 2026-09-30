import json
import os
import stat
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from lab_node_agent.host_storage import (
    StorageProbeError,
    collect_local,
    parse_lvs_report,
    read_snapshot,
    storage_pools,
    summarize,
    summarize_backing,
)

CONFIG = """
dir: local
    path /var/lib/vz
    content iso,vztmpl,backup

lvmthin: local-lvm
    thinpool data
    vgname pve
    content rootdir,images

lvmthin: student-lvm
    thinpool student-lvm
    vgname student-lvm
    content rootdir,images
"""

ROWS = [
    {
        "vg_name": "pve",
        "lv_name": "data",
        "lv_size": "67645734912",
        "data_percent": "1.50",
        "metadata_percent": "1.63",
        "lv_attr": "twi-aotz--",
        "pool_lv": "",
        "origin": "",
    },
    {
        "vg_name": "pve",
        "lv_name": "[data_tmeta]",
        "lv_size": "1073741824",
        "data_percent": "",
        "metadata_percent": "",
        "lv_attr": "ewi-ao----",
        "pool_lv": "",
        "origin": "",
    },
    {
        "vg_name": "pve",
        "lv_name": "vm-100-disk-0",
        "lv_size": "12884901888",
        "data_percent": "",
        "metadata_percent": "",
        "lv_attr": "Vwi---tz--",
        "pool_lv": "data",
        "origin": "",
    },
    {
        "vg_name": "student-lvm",
        "lv_name": "student-lvm",
        "lv_size": "489969188864",
        "data_percent": "0.21",
        "metadata_percent": "0.39",
        "lv_attr": "twi-aotz--",
        "pool_lv": "",
        "origin": "",
    },
    {
        "vg_name": "student-lvm",
        "lv_name": "vm-201-disk-0",
        "lv_size": "10737418240",
        "data_percent": "",
        "metadata_percent": "",
        "lv_attr": "Vwi---tz--",
        "pool_lv": "student-lvm",
        "origin": "",
    },
]
PVS_ROWS = [
    {
        "pv_name": "/dev/sda",
        "vg_name": "student-lvm",
        "pv_size": "500103643136",
        "pv_free": "125829120",
    },
    {"pv_name": "/dev/sdb3", "vg_name": "pve", "pv_size": "118107406336", "pv_free": "9663676416"},
]
VGS_ROWS = [
    {"vg_name": "pve", "vg_size": "118107406336", "vg_free": "9663676416", "pv_count": "1"},
    {
        "vg_name": "student-lvm",
        "vg_size": "500103643136",
        "vg_free": "125829120",
        "pv_count": "1",
    },
]


def test_pool_mapping_and_virtual_volumes_remain_unverified():
    assert storage_pools(CONFIG) == {
        "local-lvm": {"thinpool": "data", "vgname": "pve"},
        "student-lvm": {"thinpool": "student-lvm", "vgname": "student-lvm"},
    }
    pools = summarize(CONFIG, ROWS)
    assert len(pools) == 2
    assert pools[0]["metadata_percent"] == 1.63
    assert pools[1]["metadata_percent"] == 0.39
    assert pools[1]["volumes"][0]["owner_vmid_from_name"] == 201
    assert pools[1]["volumes"][0]["ownership"] == "UNVERIFIED"
    assert not pools[1]["ownership_reconciled"]


def test_missing_pool_or_metadata_fails_instead_of_reporting_free_capacity():
    with pytest.raises(StorageProbeError, match="THIN_POOL_NOT_FOUND"):
        summarize(CONFIG, ROWS[:-2])
    bad = [dict(row) for row in ROWS]
    bad[-2]["metadata_percent"] = ""
    with pytest.raises(StorageProbeError, match="INVALID_LVM_PERCENT"):
        summarize(CONFIG, bad)
    missing_link = [dict(row) for row in ROWS]
    missing_link[-1]["pool_lv"] = ""
    with pytest.raises(StorageProbeError, match="THIN_VOLUME_POOL_UNKNOWN"):
        summarize(CONFIG, missing_link)


def test_duplicate_or_malformed_storage_cannot_be_silently_skipped():
    with pytest.raises(StorageProbeError, match="DUPLICATE_OR_EXCESS_STORAGE"):
        storage_pools(CONFIG + CONFIG)
    with pytest.raises(StorageProbeError, match="INCOMPLETE_STORAGE_CONFIG"):
        storage_pools("lvmthin: student-lvm\n    vgname student-lvm\n")
    with pytest.raises(StorageProbeError, match="LVM_REPORT_INVALID"):
        parse_lvs_report(json.dumps({"report": []}).encode())


def test_pv_vg_mapping_is_observed_but_not_admission_evidence():
    pools = summarize_backing(summarize(CONFIG, ROWS), PVS_ROWS, VGS_ROWS)
    student = pools[1]
    assert student["backing"]["physical_volumes"] == [
        {"name": "/dev/sda", "size_bytes": 500103643136, "unallocated_bytes": 125829120}
    ]
    assert student["backing"]["vg_unallocated_bytes"] == 125829120
    assert student["backing"]["physical_backing_reconciled"] is False
    bad = [dict(row) for row in VGS_ROWS]
    bad[1]["pv_count"] = "2"
    with pytest.raises(StorageProbeError, match="LVM_BACKING_MISMATCH"):
        summarize_backing(summarize(CONFIG, ROWS), PVS_ROWS, bad)


def test_local_command_is_fixed_and_errors_do_not_echo_lvm_output(monkeypatch):
    from lab_node_agent import host_storage

    monkeypatch.setattr(host_storage.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(
        host_storage, "STORAGE_CONFIG", SimpleNamespace(read_text=lambda **_: CONFIG)
    )
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        section, rows = {
            host_storage.LVS: ("lv", ROWS),
            host_storage.PVS: ("pv", PVS_ROWS),
            host_storage.VGS: ("vg", VGS_ROWS),
        }[command]
        return SimpleNamespace(
            returncode=0, stdout=json.dumps({"report": [{section: rows}]}).encode()
        )

    monkeypatch.setattr(host_storage.subprocess, "run", run)
    report = collect_local()
    assert report["admission_ready"] is False
    assert report["thin_pools"][1]["volumes"][0]["name"] == "vm-201-disk-0"
    assert [call[0] for call in calls] == [host_storage.LVS, host_storage.PVS, host_storage.VGS]
    assert all(call[1]["timeout"] == 20 for call in calls)
    monkeypatch.setattr(
        host_storage.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=5, stderr=b"secret", stdout=b""),
    )
    with pytest.raises(StorageProbeError, match="^LVM_COLLECTION_FAILED$"):
        collect_local()


def test_snapshot_rejects_untrusted_file_and_expired_data(tmp_path, monkeypatch):
    if not hasattr(os, "O_NOFOLLOW"):
        pytest.skip("Linux file ownership check")
    from lab_node_agent import host_storage

    path = tmp_path / "storage.json"
    report = {
        "schema_version": 1,
        "sample_finished_at": datetime.now(UTC).isoformat(),
        "thin_pools": summarize(CONFIG, ROWS),
        "admission_ready": False,
    }
    path.write_text(json.dumps(report))
    real_fstat = os.fstat
    monkeypatch.setattr(
        host_storage.os,
        "fstat",
        lambda fd: SimpleNamespace(
            st_mode=stat.S_IFREG | 0o640,
            st_uid=0,
            st_gid=os.getegid(),
            st_size=real_fstat(fd).st_size,
        ),
    )
    assert read_snapshot(path)["thin_pools"][1]["volumes"][0]["ownership"] == "UNVERIFIED"
    with pytest.raises(StorageProbeError, match="SNAPSHOT_STALE"):
        read_snapshot(path, now=datetime.now(UTC) + timedelta(minutes=3))
    report["thin_pools"] = summarize_backing(summarize(CONFIG, ROWS), PVS_ROWS, VGS_ROWS)
    path.write_text(json.dumps(report))
    assert read_snapshot(path)["thin_pools"][1]["backing"]["physical_backing_reconciled"] is False
    report["thin_pools"][1]["backing"]["vg_unallocated_bytes"] += 1
    path.write_text(json.dumps(report))
    with pytest.raises(StorageProbeError, match="SNAPSHOT_INVALID"):
        read_snapshot(path)
    path.write_text(json.dumps(report | {"thin_pools": summarize(CONFIG, ROWS)}))
    monkeypatch.setattr(
        host_storage.os,
        "fstat",
        lambda fd: SimpleNamespace(
            st_mode=stat.S_IFREG | 0o666,
            st_uid=0,
            st_gid=os.getegid(),
            st_size=real_fstat(fd).st_size,
        ),
    )
    with pytest.raises(StorageProbeError, match="SNAPSHOT_UNTRUSTED"):
        read_snapshot(path)
