import json
import uuid
from types import SimpleNamespace

import pytest

from lab_node_agent.admission import (
    Admission,
    AdmissionManager,
    AdmissionRequest,
    network_readiness,
    verify_lxc,
)
from lab_node_agent.segments import SegmentError, SegmentSpec


def test_live_network_readiness_closes_on_missing_firewall_unit(tmp_path):
    (tmp_path / "vmbr1" / "bridge").mkdir(parents=True)
    policy = tmp_path / "egress.nft"
    policy.write_text('iifname "vmbr1" ip saddr 10.60.0.10 drop\n')
    policy.chmod(0o600)
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0)

    arguments = dict(
        source=lambda: {"address": "10.60.0.10", "bridge": "vmbr1"},
        run=run,
        bridge_root=tmp_path,
        policy=policy,
    )
    assert network_readiness(**arguments) == {
        "ready": True,
        "guacamole": {"address": "10.60.0.10", "bridge": "vmbr1"},
    }
    assert len(commands) == 7
    assert network_readiness(
        **{**arguments, "run": lambda *_args, **_kwargs: SimpleNamespace(returncode=1)}
    ) == {"ready": False}


def fixture():
    allocation_id = uuid.uuid4()
    runtime_id = uuid.uuid4()
    segment = SegmentSpec.parse(
        {"allocation_id": str(allocation_id), "mode": "ISOLATED", "cidr": "10.70.4.0/30"}
    )
    admission = Admission.parse(
        {
            "allocation_id": str(allocation_id),
            "runtime_id": str(runtime_id),
            "generation": 2,
            "vmid": 901001,
            "address": "10.70.4.2",
            "mac": "bc:24:11:aa:bb:cc",
        }
    )
    config = {
        "description": f"lab-manager:runtime={runtime_id};generation=2",
        "unprivileged": "1",
        "onboot": "0",
        "ostype": "debian",
        "net0": (
            f"name=eth0,bridge={segment.bridge},firewall=1,hwaddr=BC:24:11:AA:BB:CC,"
            "ip=10.70.4.2/30,gw=10.70.4.1,ip6=manual,link_down=1,type=veth"
        ),
    }
    return segment, admission, config


class Segments:
    def __init__(self, spec):
        self.spec = spec

    def read(self):
        return {str(self.spec.allocation_id): self.spec.record()}

    def link(self, spec):
        assert spec == self.spec
        return {"flags": ["UP"]}

    def _address(self, spec):
        assert spec == self.spec
        return [{"family": "inet", "local": "10.70.4.1", "prefixlen": 30}]

    @staticmethod
    def _check_gateway_address(spec, addresses):
        assert spec.gateway == "10.70.4.1/30" and len(addresses) == 1


def test_lxc_identity_is_required_both_before_and_after_link_enable():
    spec, admission, config = fixture()
    verify_lxc(admission, spec, config)
    config["net0"] = config["net0"].replace(",link_down=1", "")
    verify_lxc(admission, spec, config)
    for changed in (
        {**config, "description": "lab-manager:runtime=" + str(uuid.uuid4()) + ";generation=2"},
        {**config, "net0": config["net0"].replace("BC:24:11:AA:BB:CC", "BC:24:11:AA:BB:CD")},
        {**config, "net0": config["net0"] + ",link_down=0"},
    ):
        with pytest.raises(SegmentError):
            verify_lxc(admission, spec, changed)


def test_admission_is_fail_closed_and_revocation_survives_restart(tmp_path):
    spec, admission, config = fixture()
    applied = []
    subject = AdmissionManager(
        Segments(spec),
        state_file=tmp_path / "admissions.json",
        source=lambda: {"address": "10.60.0.10", "bridge": "vmbr1"},
        config=lambda vmid: config if vmid == admission.vmid else {},
        firewall=applied.append,
    )
    request = AdmissionRequest.parse(
        {key: value for key, value in admission.record().items() if key != "mac"}
    )
    assert subject.admit(request)["vmid"] == admission.vmid
    assert len(applied) == 2
    assert 'iifname "lmbr*" drop' in applied[0]
    assert "10.60.0.10" in applied[1] and "10.70.4.2" in applied[1]
    assert list(subject.read().values()) == [admission]
    subject.restore()
    assert applied[-1] == applied[1]
    assert subject.revoke(admission.allocation_id)
    assert subject.read() == {}
    subject.restore()
    assert "10.60.0.10" not in applied[-1]
    assert applied[-1].count('iifname "lmbr*" drop') == 2


def test_failed_admission_does_not_persist_or_open_firewall(tmp_path):
    spec, admission, config = fixture()
    config["net0"] = config["net0"].replace("10.70.4.2", "10.70.4.3")
    applied = []
    subject = AdmissionManager(
        Segments(spec),
        state_file=tmp_path / "admissions.json",
        source=lambda: {"address": "10.60.0.10", "bridge": "vmbr1"},
        config=lambda _vmid: config,
        firewall=applied.append,
    )
    with pytest.raises(SegmentError, match="GUEST_NETWORK_DRIFT"):
        subject.admit(
            AdmissionRequest.parse(
                {key: value for key, value in admission.record().items() if key != "mac"}
            )
        )
    assert applied == []
    assert not subject.state_file.exists()


def test_duplicate_state_entry_fails_closed(tmp_path):
    spec, admission, config = fixture()
    path = tmp_path / "admissions.json"
    path.write_text(json.dumps({"version": 1, "admissions": {"wrong": admission.record()}}))
    subject = AdmissionManager(Segments(spec), state_file=path)
    with pytest.raises(SegmentError, match="ADMISSION_STATE_INVALID"):
        subject.read()
