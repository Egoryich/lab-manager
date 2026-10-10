import json
import ssl
import urllib.error
import uuid
from types import SimpleNamespace

import pytest

from lab_node_agent.lxc_provider import CreateLxc, LxcOperationError, ProxmoxLxcProvider
from lab_node_agent.proxmox import ProxmoxConfig

TASK = "UPID:pve:00000100:00000010:60000000:vzcreate:200:lab@pve:"


class Response:
    status = 200

    def __init__(self, data):
        self.body = json.dumps({"data": data}).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit):
        return self.body[:limit]


class FakeOpener:
    def __init__(self, results):
        self.results = iter(results)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        item = next(self.results)
        if item is None:
            raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)
        return Response(item)


def provider(tmp_path, monkeypatch, results):
    ca = tmp_path / "ca.pem"
    ca.write_text("test")
    monkeypatch.setattr(ssl, "create_default_context", lambda **kwargs: SimpleNamespace())
    subject = ProxmoxLxcProvider(
        ProxmoxConfig("https://127.0.0.1:8006", "pve", ca, "lab@pve!write", "x" * 32)
    )
    subject.opener = FakeOpener(results)
    return subject


def spec(**changes):
    values = {
        "runtime_id": uuid.uuid4(),
        "generation": 1,
        "vmid": 200,
        "hostname": "lab-student-01",
        "template": "local:vztmpl/debian-13-standard_13.6-1_amd64.tar.zst",
        "storage": "student-lvm",
        "pool": "lab-manager",
        "bridge": "lmbrabc123",
        "cidr": "10.70.1.0/29",
        "address": "10.70.1.2",
        "memory_mib": 1024,
        "cores": 1,
        "disk_gib": 10,
        "ssh_public_key": "ssh-ed25519 " + "A" * 68,
    }
    values.update(changes)
    return CreateLxc(**values)


def test_create_submits_unprivileged_stopped_debian_with_closed_link(tmp_path, monkeypatch):
    subject = provider(tmp_path, monkeypatch, [None, TASK])
    guest = spec()
    assert subject.create(guest) == TASK
    assert [r.get_method() for r in subject.opener.requests] == ["GET", "POST"]
    request = subject.opener.requests[-1]
    from urllib.parse import parse_qs

    form = {key: item[0] for key, item in parse_qs(request.data.decode()).items()}
    assert form["vmid"] == "200"
    assert form["ostemplate"] == guest.template
    assert form["rootfs"] == "student-lvm:10"
    assert form["pool"] == "lab-manager"
    assert form["unprivileged"] == "1"
    assert form["start"] == form["onboot"] == "0"
    assert "link_down=1" in form["net0"]
    assert "ip=10.70.1.2/29,gw=10.70.1.1" in form["net0"]
    assert "ip6=manual" in form["net0"]
    assert form["description"] == guest.marker
    assert form["ssh-public-keys"] == guest.ssh_public_key


def test_retry_never_creates_second_guest_or_adopts_foreign_vmid(tmp_path, monkeypatch):
    guest = spec()
    existing = {
        "description": guest.marker,
        "unprivileged": 1,
        "onboot": 0,
        "ostype": "debian",
        "hostname": guest.hostname,
        "memory": guest.memory_mib,
        "cores": guest.cores,
        "swap": 0,
        "rootfs": f"student-lvm:vm-{guest.vmid}-disk-0,size={guest.disk_gib}G",
        "net0": (
            f"name=eth0,bridge={guest.bridge},firewall=1,hwaddr=BC:24:11:00:00:00,"
            "ip=10.70.1.2/29,gw=10.70.1.1,ip6=manual,link_down=1,type=veth"
        ),
    }
    subject = provider(tmp_path, monkeypatch, [existing])
    assert subject.create(guest) is None
    assert len(subject.opener.requests) == 1

    subject = provider(tmp_path, monkeypatch, [{**existing, "description": guest.marker + "\n"}])
    assert subject.create(guest) is None
    assert len(subject.opener.requests) == 1

    subject = provider(tmp_path, monkeypatch, [{**existing, "description": "foreign"}])
    with pytest.raises(LxcOperationError, match="VMID_ALREADY_OWNED"):
        subject.create(guest)
    assert len(subject.opener.requests) == 1

    subject = provider(tmp_path, monkeypatch, [{**existing, "description": " " + guest.marker}])
    with pytest.raises(LxcOperationError, match="VMID_ALREADY_OWNED"):
        subject.create(guest)

    subject = provider(tmp_path, monkeypatch, [{**existing, "net0": "bridge=vmbr0"}])
    with pytest.raises(LxcOperationError, match="GUEST_CONFIGURATION_DRIFT"):
        subject.create(guest)

    for change in (
        {"rootfs": "local-lvm:vm-200-disk-0,size=10G"},
        {"rootfs": "student-lvm:vm-200-disk-0,size=20G"},
        {"net0": existing["net0"].replace("link_down=1", "link_down=0")},
        {"net0": existing["net0"].replace("firewall=1", "firewall=0")},
        {"net0": existing["net0"].replace("ip=10.70.1.2/29", "ip=10.70.1.3/29")},
    ):
        subject = provider(tmp_path, monkeypatch, [{**existing, **change}])
        with pytest.raises(LxcOperationError, match="GUEST_CONFIGURATION_DRIFT"):
            subject.create(guest)


def test_start_and_shutdown_require_owned_unprivileged_guest(tmp_path, monkeypatch):
    guest = spec()
    config = {
        "description": guest.marker,
        "unprivileged": 1,
        "onboot": 0,
        "net0": (
            f"name=eth0,bridge={guest.bridge},firewall=1,hwaddr=BC:24:11:00:00:00,"
            "ip=10.70.1.2/29,gw=10.70.1.1,ip6=manual,link_down=1,type=veth"
        ),
    }
    enabled = {**config, "net0": config["net0"].replace(",link_down=1", "")}
    subject = provider(tmp_path, monkeypatch, [config, {}, enabled, TASK, config, TASK])
    assert subject.start(guest.vmid, guest.runtime_id, guest.generation) == TASK
    assert subject.shutdown(guest.vmid, guest.runtime_id, guest.generation) == TASK
    assert [r.get_method() for r in subject.opener.requests] == [
        "GET",
        "PUT",
        "GET",
        "POST",
        "GET",
        "POST",
    ]
    assert subject.opener.requests[1].full_url.endswith("/lxc/200/config")
    assert b"link_down" not in subject.opener.requests[1].data
    assert subject.opener.requests[3].full_url.endswith("/lxc/200/status/start")
    assert subject.opener.requests[5].full_url.endswith("/lxc/200/status/shutdown")
    assert subject.opener.requests[5].data == b"timeout=60"

    subject = provider(
        tmp_path, monkeypatch, [{**config, "description": guest.marker + "\n"}, {}, enabled, TASK]
    )
    assert subject.start(guest.vmid, guest.runtime_id, guest.generation) == TASK

    subject = provider(tmp_path, monkeypatch, [{**config, "net0": "bridge=vmbr0"}])
    with pytest.raises(LxcOperationError, match="GUEST_CONFIGURATION_DRIFT"):
        subject.start(guest.vmid, guest.runtime_id, guest.generation)
    assert len(subject.opener.requests) == 1

    subject = provider(tmp_path, monkeypatch, [config, {}, config])
    with pytest.raises(LxcOperationError, match="GUEST_NETWORK_DRIFT"):
        subject.start(guest.vmid, guest.runtime_id, guest.generation)
    assert [r.get_method() for r in subject.opener.requests] == ["GET", "PUT", "GET"]


def test_task_status_reads_only_a_valid_task_id(tmp_path, monkeypatch):
    subject = provider(tmp_path, monkeypatch, [{"status": "stopped", "exitstatus": "OK"}])
    assert subject.task_status(TASK)["exitstatus"] == "OK"
    request = subject.opener.requests[0]
    assert "/tasks/UPID%3Apve%3A" in request.full_url
    assert request.get_method() == "GET"
    with pytest.raises(LxcOperationError, match="PROXMOX_TASK_INVALID"):
        subject.task_status("../tasks/other")
    assert len(subject.opener.requests) == 1


def test_current_status_uses_bounded_lxc_path(tmp_path, monkeypatch):
    subject = provider(tmp_path, monkeypatch, [{"status": "running"}])
    assert subject.current_status(200) == "running"
    assert subject.opener.requests[0].full_url.endswith("/lxc/200/status/current")


@pytest.mark.parametrize(
    "change",
    [
        {"template": "local:vztmpl/ubuntu-24.tar.zst"},
        {"template": "local:vztmpl/../debian-13.tar.zst"},
        {"bridge": "vmbr0"},
        {"pool": "../other"},
        {"vmid": True},
        {"ssh_public_key": "not-a-key"},
        {"address": "192.168.0.123"},
        {"address": "10.70.1.1"},
        {"address": "10.70.1.0"},
        {"cidr": "8.8.8.0/24"},
    ],
)
def test_unsafe_lxc_spec_rejected(change):
    with pytest.raises(LxcOperationError, match="INVALID_LXC_SPEC"):
        spec(**change)
