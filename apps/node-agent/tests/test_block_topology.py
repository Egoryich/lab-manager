import copy
import json

import pytest
from test_host_storage import BLOCK_TREE, CONFIG, PVS_ROWS, ROOT_MOUNT, ROWS, VGS_ROWS

from lab_node_agent.block_topology import TopologyError, add_topology
from lab_node_agent.host_storage import summarize, summarize_backing


def pools():
    return summarize_backing(summarize(CONFIG, ROWS), PVS_ROWS, VGS_ROWS)


def test_student_pv_is_whole_disk_separate_from_system_root():
    result = add_topology(pools(), json.dumps(BLOCK_TREE).encode(), json.dumps(ROOT_MOUNT).encode())
    system = result[0]["backing"]["physical_volumes"][0]["topology"]
    student = result[1]["backing"]["physical_volumes"][0]["topology"]
    assert system["type"] == "part"
    assert system["backing_disks"] == ["/dev/sdb"]
    assert system["shares_system_disk"] is True
    assert student["type"] == "disk"
    assert student["backing_disks"] == ["/dev/sda"]
    assert student["whole_disk"] is True
    assert student["shares_system_disk"] is False
    assert result[1]["backing"]["physical_backing_reconciled"] is False


def test_missing_pv_or_mismatched_root_fails_closed():
    tree = copy.deepcopy(BLOCK_TREE)
    tree["blockdevices"][0]["path"] = "/dev/other"
    with pytest.raises(TopologyError, match="PV_BLOCK_DEVICE_UNKNOWN"):
        add_topology(pools(), json.dumps(tree).encode(), json.dumps(ROOT_MOUNT).encode())
    mount = {"filesystems": [{"source": "/dev/mapper/other-root", "target": "/"}]}
    with pytest.raises(TopologyError, match="ROOT_DEVICE_UNKNOWN"):
        add_topology(pools(), json.dumps(BLOCK_TREE).encode(), json.dumps(mount).encode())


def test_smaller_block_device_and_conflicting_duplicate_fail_closed():
    tree = copy.deepcopy(BLOCK_TREE)
    tree["blockdevices"][0]["size"] = 499000000000
    with pytest.raises(TopologyError, match="PV_BLOCK_DEVICE_UNKNOWN"):
        add_topology(pools(), json.dumps(tree).encode(), json.dumps(ROOT_MOUNT).encode())
    tree = copy.deepcopy(BLOCK_TREE)
    tree["blockdevices"][0]["children"][1]["size"] -= 1
    with pytest.raises(TopologyError, match="BLOCK_PATH_CONFLICT"):
        add_topology(pools(), json.dumps(tree).encode(), json.dumps(ROOT_MOUNT).encode())
