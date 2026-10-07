import json
import uuid

import pytest

from lab_node_agent.network_policy import SegmentMode
from lab_node_agent.segment_daemon import restore_bridges
from lab_node_agent.segments import SegmentError, SegmentManager, SegmentSpec


class FakeIP:
    def __init__(self):
        self.links = {}
        self.ports = {}
        self.addresses = {}
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        if args[:3] == ("link", "add", "name"):
            self.links[args[3]] = {"linkinfo": {"info_kind": "bridge"}, "flags": []}
            return ""
        if args[:3] == ("link", "set", "dev"):
            if args[4] == "alias":
                self.links[args[3]]["ifalias"] = args[5]
            elif args[4] in ("up", "down"):
                self.links[args[3]]["flags"] = ["UP"] if args[4] == "up" else []
            else:
                raise AssertionError(args)
            return ""
        if args[:2] == ("address", "add"):
            local, prefix = args[2].split("/")
            self.addresses[args[4]] = [{"family": "inet", "local": local, "prefixlen": int(prefix)}]
            return ""
        if args[:2] == ("address", "del"):
            self.addresses[args[4]] = []
            return ""
        if args[:3] == ("link", "delete", "dev"):
            del self.links[args[3]]
            return ""
        if args[:5] == ("-j", "-d", "link", "show", "dev"):
            return json.dumps([self.links[args[5]]])
        if args[:4] == ("-j", "link", "show", "master"):
            return json.dumps(self.ports.get(args[4], []))
        if args[:4] == ("-j", "address", "show", "dev"):
            return json.dumps([{"addr_info": self.addresses.get(args[4], [])}])
        raise AssertionError(args)


def spec(allocation_id=None, mode=SegmentMode.ISOLATED, cidr="10.70.1.0/30"):
    return SegmentSpec(allocation_id or uuid.uuid4(), mode, cidr)


def manager(tmp_path, fake):
    return SegmentManager(tmp_path, command=fake, link_exists=lambda name: name in fake.links)


def test_create_is_idempotent_and_delete_removes_only_empty_owned_bridge(tmp_path):
    fake = FakeIP()
    subject = manager(tmp_path, fake)
    segment = spec()
    assert subject.create(segment) == segment.bridge
    assert subject.create(segment) == segment.bridge
    assert len([call for call in fake.calls if call[:2] == ("link", "add")]) == 1
    assert subject.read()[str(segment.allocation_id)] == segment.record()
    assert subject.delete(segment.allocation_id) is True
    assert subject.delete(segment.allocation_id) is False
    assert segment.bridge not in fake.links


def test_existing_unowned_bridge_is_never_adopted(tmp_path):
    fake = FakeIP()
    segment = spec()
    fake.links[segment.bridge] = {"linkinfo": {"info_kind": "bridge"}, "flags": []}
    with pytest.raises(SegmentError, match="BRIDGE_ALREADY_EXISTS"):
        manager(tmp_path, fake).create(segment)


def test_attached_port_or_address_prevents_delete(tmp_path):
    fake = FakeIP()
    subject = manager(tmp_path, fake)
    segment = spec()
    subject.create(segment)
    fake.ports[segment.bridge] = [{"ifname": "tap123i0"}]
    with pytest.raises(SegmentError, match="BRIDGE_IN_USE"):
        subject.delete(segment.allocation_id)
    fake.ports.clear()
    fake.addresses[segment.bridge] = [{"local": "10.70.1.1"}]
    with pytest.raises(SegmentError, match="BRIDGE_IN_USE"):
        subject.delete(segment.allocation_id)
    assert segment.bridge in fake.links


def test_up_bridge_prevents_delete(tmp_path):
    fake = FakeIP()
    subject = manager(tmp_path, fake)
    segment = spec()
    subject.create(segment)
    fake.links[segment.bridge]["flags"] = ["BROADCAST", "UP"]
    with pytest.raises(SegmentError, match="BRIDGE_STILL_UP"):
        subject.delete(segment.allocation_id)
    assert segment.bridge in fake.links


def test_prepare_and_close_gateway_is_idempotent_and_keeps_owned_bridge(tmp_path):
    fake = FakeIP()
    subject = manager(tmp_path, fake)
    segment = spec()
    subject.create(segment)
    assert subject.prepare_gateway(segment.allocation_id) == "10.70.1.1/30"
    assert subject.prepare_gateway(segment.allocation_id) == "10.70.1.1/30"
    assert fake.links[segment.bridge]["flags"] == ["UP"]
    assert fake.addresses[segment.bridge] == [
        {"family": "inet", "local": "10.70.1.1", "prefixlen": 30}
    ]
    assert subject.close_gateway(segment.allocation_id)
    assert subject.close_gateway(segment.allocation_id)
    assert fake.links[segment.bridge]["flags"] == []
    assert fake.addresses[segment.bridge] == []
    assert segment.bridge in fake.links
    assert len([call for call in fake.calls if call[:2] == ("address", "add")]) == 1


def test_prepare_rejects_foreign_address_and_close_rejects_attached_port(tmp_path):
    fake = FakeIP()
    subject = manager(tmp_path, fake)
    segment = spec()
    subject.create(segment)
    fake.addresses[segment.bridge] = [{"family": "inet", "local": "10.70.1.2", "prefixlen": 30}]
    with pytest.raises(SegmentError, match="BRIDGE_ADDRESS_CONFLICT"):
        subject.prepare_gateway(segment.allocation_id)
    fake.addresses[segment.bridge] = []
    fake.ports[segment.bridge] = [{"ifname": "veth123"}]
    with pytest.raises(SegmentError, match="BRIDGE_IN_USE"):
        subject.prepare_gateway(segment.allocation_id)
    with pytest.raises(SegmentError, match="BRIDGE_IN_USE"):
        subject.close_gateway(segment.allocation_id)


def test_corrupt_state_blocks_mutations(tmp_path):
    fake = FakeIP()
    subject = manager(tmp_path, fake)
    subject.state_file.write_text('{"version":1,"segments":{"bad":{}}}')
    with pytest.raises(SegmentError, match="INVALID_SEGMENT_STATE"):
        subject.create(spec())
    assert fake.links == {}


def test_short_bridge_name_collision_is_rejected(tmp_path, monkeypatch):
    fake = FakeIP()
    subject = manager(tmp_path, fake)
    first = spec(uuid.UUID("12345678-9ab0-4000-8000-000000000001"))
    second = spec(uuid.UUID("12345678-9ab0-4000-8000-000000000002"))
    monkeypatch.setattr(SegmentSpec, "bridge", property(lambda self: "lmbrtest"))
    assert first.bridge == second.bridge
    subject.create(first)
    with pytest.raises(SegmentError, match="BRIDGE_NAME_COLLISION"):
        subject.create(second)


def test_segment_cidr_overlap_is_rejected_before_creating_second_bridge(tmp_path):
    fake = FakeIP()
    subject = manager(tmp_path, fake)
    subject.create(spec(cidr="10.70.8.0/29"))
    with pytest.raises(SegmentError, match="SEGMENT_CIDR_COLLISION"):
        subject.create(spec(cidr="10.70.8.0/30"))
    assert len(fake.links) == 1


def test_bridge_name_fits_proxmox_limit():
    bridge = spec().bridge
    assert len(bridge) == 10
    assert bridge.startswith("lmbr")


def test_changed_spec_or_owner_blocks_repeated_create_and_delete(tmp_path):
    fake = FakeIP()
    subject = manager(tmp_path, fake)
    segment = spec()
    subject.create(segment)
    with pytest.raises(SegmentError, match="SEGMENT_SPEC_CONFLICT"):
        subject.create(spec(segment.allocation_id, SegmentMode.GROUP_LAN))
    fake.links[segment.bridge]["ifalias"] = "someone-else"
    with pytest.raises(SegmentError, match="BRIDGE_OWNER_MISMATCH"):
        subject.delete(segment.allocation_id)


def test_partial_create_can_resume_only_with_matching_alias(tmp_path):
    fake = FakeIP()
    subject = manager(tmp_path, fake)
    segment = spec()
    subject.write({str(segment.allocation_id): segment.record()})
    assert subject.create(segment) == segment.bridge
    assert subject.create(segment) == segment.bridge


def test_startup_restores_missing_bridge_without_changing_live_one(tmp_path):
    fake = FakeIP()
    subject = manager(tmp_path, fake)
    missing = spec()
    live = spec(cidr="10.70.1.4/30")
    subject.write(
        {
            str(missing.allocation_id): missing.record(),
            str(live.allocation_id): live.record(),
        }
    )
    fake.links[live.bridge] = {
        "linkinfo": {"info_kind": "bridge"},
        "ifalias": live.alias,
        "flags": ["UP"],
    }
    restore_bridges(subject)
    assert subject.link(missing) is not None
    assert fake.links[live.bridge]["flags"] == ["UP"]
    assert sum(call[:2] == ("link", "add") for call in fake.calls) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {
            "allocation_id": str(uuid.uuid4()),
            "mode": "GROUP_LAN",
            "cidr": "192.168.0.0/24",
            "extra": 1,
        },
        {"allocation_id": str(uuid.uuid4()), "mode": "INVALID", "cidr": "10.70.1.0/30"},
        {"allocation_id": str(uuid.uuid4()), "mode": "ISOLATED", "cidr": "10.70.1.1/30"},
        {"allocation_id": str(uuid.uuid4()), "mode": "ISOLATED", "cidr": "100.64.0.0/30"},
        {"allocation_id": str(uuid.uuid4()), "mode": "ISOLATED", "cidr": "192.168.0.0/30"},
    ],
)
def test_invalid_specs_are_rejected(payload):
    with pytest.raises(SegmentError):
        SegmentSpec.parse(payload)
