import json
from types import SimpleNamespace

import pytest

from lab_node_agent.host_storage import (
    StorageProbeError,
    collect_local,
    parse_lvs_report,
    storage_pools,
    summarize,
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


def test_duplicate_or_malformed_storage_cannot_be_silently_skipped():
    with pytest.raises(StorageProbeError, match="DUPLICATE_OR_EXCESS_STORAGE"):
        storage_pools(CONFIG + CONFIG)
    with pytest.raises(StorageProbeError, match="INCOMPLETE_STORAGE_CONFIG"):
        storage_pools("lvmthin: student-lvm\n    vgname student-lvm\n")
    with pytest.raises(StorageProbeError, match="LVM_REPORT_INVALID"):
        parse_lvs_report(json.dumps({"report": []}).encode())


def test_local_command_is_fixed_and_errors_do_not_echo_lvm_output(monkeypatch):
    from lab_node_agent import host_storage

    monkeypatch.setattr(host_storage.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(
        host_storage, "STORAGE_CONFIG", SimpleNamespace(read_text=lambda **_: CONFIG)
    )
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps({"report": [{"lv": ROWS}]}).encode())

    monkeypatch.setattr(host_storage.subprocess, "run", run)
    report = collect_local()
    assert report["admission_ready"] is False
    assert report["thin_pools"][1]["volumes"][0]["name"] == "vm-201-disk-0"
    assert calls[0][0] == host_storage.LVS
    assert calls[0][1]["timeout"] == 20
    monkeypatch.setattr(
        host_storage.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=5, stderr=b"secret", stdout=b""),
    )
    with pytest.raises(StorageProbeError, match="^LVM_COLLECTION_FAILED$"):
        collect_local()
