import json
import uuid

import pytest

from lab_node_agent.segment_daemon import dispatch, restore_bridges
from lab_node_agent.segments import SegmentError, SegmentSpec


class Manager:
    def __init__(self):
        self.records = {}

    def create(self, spec):
        self.records[str(spec.allocation_id)] = spec.record()
        return spec.bridge

    def read(self):
        return self.records

    def link(self, spec):
        return {"ifname": spec.bridge}

    def prepare_gateway(self, allocation_id):
        record = self.records[str(allocation_id)]
        return SegmentSpec.parse(
            {field: record[field] for field in ("allocation_id", "mode", "cidr")}
        ).gateway

    def close_gateway(self, allocation_id):
        assert str(allocation_id) in self.records
        return True


def test_segment_helper_accepts_only_typed_create_and_status():
    manager = Manager()
    allocation_id = uuid.uuid4()
    request = {
        "action": "create",
        "spec": {
            "allocation_id": str(allocation_id),
            "mode": "ISOLATED",
            "cidr": "10.70.1.0/29",
        },
    }
    created = dispatch(json.dumps(request).encode(), manager)
    assert created == {**SegmentSpec.parse(request["spec"]).record(), "state": "CREATED"}
    assert (
        dispatch(
            json.dumps({"action": "get", "allocation_id": str(allocation_id)}).encode(), manager
        )
        == created
    )
    prepared = dispatch(
        json.dumps({"action": "prepare_gateway", "allocation_id": str(allocation_id)}).encode(),
        manager,
    )
    assert prepared == {**created, "gateway": "10.70.1.1/29", "state": "GATEWAY_PREPARED"}
    assert (
        dispatch(
            json.dumps({"action": "close_gateway", "allocation_id": str(allocation_id)}).encode(),
            manager,
        )
        == created
    )
    with pytest.raises(SegmentError, match="INVALID_SEGMENT_REQUEST"):
        dispatch(b'{"action":"create","action":"get"}', manager)
    with pytest.raises(SegmentError, match="INVALID_SEGMENT_REQUEST"):
        dispatch(json.dumps({**request, "command": "ip link set vmbr0 down"}).encode(), manager)
    with pytest.raises(SegmentError, match="SEGMENT_NOT_FOUND"):
        dispatch(
            json.dumps({"action": "get", "allocation_id": str(uuid.uuid4())}).encode(), manager
        )


def test_startup_recreates_persisted_bridges_and_reports_live_state():
    manager = Manager()
    allocation_id = uuid.uuid4()
    spec = SegmentSpec.parse(
        {"allocation_id": str(allocation_id), "mode": "GROUP_LAN", "cidr": "10.70.1.0/29"}
    )
    manager.records[str(allocation_id)] = spec.record()
    restore_bridges(manager)
    assert manager.link(spec) == {"ifname": spec.bridge}

    manager.link = lambda _spec: {"ifname": spec.bridge, "flags": ["UP"]}
    result = dispatch(
        json.dumps({"action": "get", "allocation_id": str(allocation_id)}).encode(), manager
    )
    assert result["state"] == "ACTIVE"


def test_helper_dispatches_only_typed_ssh_admission():
    class Admissions:
        def __init__(self):
            self.calls = []

        def admit(self, admission):
            self.calls.append(("admit", admission))
            return admission.record()

        def revoke(self, allocation_id):
            self.calls.append(("revoke", allocation_id))
            return True

    allocation_id = uuid.uuid4()
    payload = {
        "allocation_id": str(allocation_id),
        "runtime_id": str(uuid.uuid4()),
        "generation": 1,
        "vmid": 901001,
        "address": "10.70.4.2",
        "mac": "bc:24:11:aa:bb:cc",
    }
    admissions = Admissions()
    admitted = dispatch(
        json.dumps({"action": "admit_ssh", "admission": payload}).encode(),
        Manager(),
        admissions,
    )
    assert admitted == {**payload, "state": "APPLIED"}
    revoked = dispatch(
        json.dumps({"action": "revoke_ssh", "allocation_id": str(allocation_id)}).encode(),
        Manager(),
        admissions,
    )
    assert revoked == {"allocation_id": str(allocation_id), "state": "REVOKED"}
    assert [call[0] for call in admissions.calls] == ["admit", "revoke"]
    with pytest.raises(SegmentError, match="INVALID_SEGMENT_REQUEST"):
        dispatch(json.dumps({"action": "admit_ssh", "admission": payload}).encode(), Manager())
