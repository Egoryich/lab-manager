import json
import uuid

import pytest

from lab_node_agent.command_dispatch import CommandDispatcher, CommandError, inspect_lab_bridge
from lab_node_agent.command_journal import CommandJournal
from lab_node_agent.lxc_provider import LxcOperationError
from lab_node_agent.segments import SegmentSpec


class Provider:
    def __init__(self, task="UPID"):
        self.task = task
        self.calls = []
        self.task_result = {"status": "running"}
        self.config = None
        self.guest_status = "stopped"

    def create(self, spec):
        self.calls.append(("create", spec))
        if isinstance(self.task, Exception):
            raise self.task
        return self.task

    def start(self, vmid, runtime_id, generation):
        self.calls.append(("start", vmid, runtime_id, generation))
        return self.task

    def shutdown(self, vmid, runtime_id, generation):
        self.calls.append(("shutdown", vmid, runtime_id, generation))
        return self.task

    def task_status(self, task_id):
        self.calls.append(("status", task_id))
        return self.task_result

    def config_for(self, vmid):
        self.calls.append(("config", vmid))
        return self.config

    def current_status(self, vmid):
        self.calls.append(("current", vmid))
        return self.guest_status


def command(node_id, *, kind="LXC_CREATE"):
    payload = {
        "operation_id": str(uuid.uuid4()),
        "node_id": str(node_id),
        "kind": kind,
        "runtime_id": str(uuid.uuid4()),
        "generation": 1,
        "vmid": 901001,
    }
    if kind == "LXC_CREATE":
        payload["spec"] = {
            "hostname": "lab-student-01",
            "template": "local:vztmpl/debian-13-standard_13.6-1_amd64.tar.zst",
            "storage": "student-lvm",
            "segment": {
                "allocation_id": str(uuid.uuid4()),
                "mode": "ISOLATED",
                "cidr": "10.70.0.0/24",
            },
            "address": "10.70.0.2",
            "memory_mib": 512,
            "cores": 1,
            "disk_gib": 4,
            "ssh_public_key": "ssh-ed25519 " + "A" * 68,
        }
    return payload


def dispatcher(tmp_path, node_id, provider, inspected):
    journal = CommandJournal(tmp_path / "commands.sqlite3")
    return CommandDispatcher(
        node_id=node_id,
        template="local:vztmpl/debian-13-standard_13.6-1_amd64.tar.zst",
        storage="student-lvm",
        pool="lab-manager",
        journal=journal,
        provider=provider,
        inspect_bridge=lambda spec: inspected.append(spec),
    )


def encode(payload):
    return json.dumps(payload).encode()


def test_create_commits_receipt_and_never_resubmits_on_retry_or_restart(tmp_path):
    node_id = uuid.uuid4()
    provider = Provider()
    inspected = []
    payload = command(node_id)
    first = dispatcher(tmp_path, node_id, provider, inspected)
    result = first.submit(encode(payload))
    assert result["state"] == "SUBMITTED"
    assert result["task_id"] == "UPID"
    assert len(provider.calls) == len(inspected) == 1
    first.journal.close()

    second = dispatcher(tmp_path, node_id, provider, inspected)
    assert second.submit(encode(payload)) == result
    assert len(provider.calls) == len(inspected) == 1
    second.journal.close()


def test_lost_response_is_uncertain_and_not_replayed(tmp_path):
    node_id = uuid.uuid4()
    provider = Provider(LxcOperationError("PROXMOX_CONNECTION_FAILED"))
    subject = dispatcher(tmp_path, node_id, provider, [])
    payload = command(node_id)
    first = subject.submit(encode(payload))
    assert first["state"] == "UNCERTAIN"
    assert first["error_code"] == "PROXMOX_CONNECTION_FAILED"
    assert subject.submit(encode(payload)) == first
    assert len(provider.calls) == 1
    subject.journal.close()


def test_task_success_requires_external_reconciliation(tmp_path):
    node_id = uuid.uuid4()
    provider = Provider()
    subject = dispatcher(tmp_path, node_id, provider, [])
    payload = command(node_id, kind="LXC_START")
    subject.submit(encode(payload))
    provider.task_result = {"status": "stopped", "exitstatus": "OK"}
    result = subject.status(uuid.UUID(payload["operation_id"]))
    assert result["state"] == "SUBMITTED"
    assert result["task_status"] == "OK"
    provider.task_result = {"status": "stopped", "exitstatus": "ERROR"}
    result = subject.status(uuid.UUID(payload["operation_id"]))
    assert result["state"] == "UNCERTAIN"
    subject.journal.close()


def test_task_success_only_after_owned_guest_state_confirmed(tmp_path):
    node_id = uuid.uuid4()
    provider = Provider()
    subject = dispatcher(tmp_path, node_id, provider, [])
    payload = command(node_id, kind="LXC_START")
    subject.submit(encode(payload))
    provider.task_result = {"status": "stopped", "exitstatus": "OK"}
    provider.config = {
        "description": f"lab-manager:runtime={payload['runtime_id']};generation=1\n",
        "unprivileged": 1,
        "onboot": 0,
        "net0": "name=eth0,bridge=lmbrabc123,firewall=1",
    }
    provider.guest_status = "running"
    result = subject.status(uuid.UUID(payload["operation_id"]))
    assert result["state"] == "SUCCEEDED"
    subject.journal.close()


def test_create_reconciliation_checks_exact_storage_size_and_bridge(tmp_path):
    node_id = uuid.uuid4()
    provider = Provider()
    subject = dispatcher(tmp_path, node_id, provider, [])
    payload = command(node_id)
    bridge = SegmentSpec.parse(payload["spec"]["segment"]).bridge
    subject.submit(encode(payload))
    provider.task_result = {"status": "stopped", "exitstatus": "OK"}
    provider.config = {
        "description": f"lab-manager:runtime={payload['runtime_id']};generation=1\n",
        "unprivileged": 1,
        "onboot": 0,
        "ostype": "debian",
        "hostname": payload["spec"]["hostname"],
        "memory": payload["spec"]["memory_mib"],
        "cores": payload["spec"]["cores"],
        "net0": (
            f"name=eth0,bridge={bridge},firewall=1,"
            "ip=10.70.0.2/24,gw=10.70.0.1,ip6=manual,link_down=1"
        ),
        "rootfs": "local-lvm:vm-901001-disk-0,size=4G",
    }
    operation_id = uuid.UUID(payload["operation_id"])
    assert subject.status(operation_id)["state"] == "SUBMITTED"
    provider.config["rootfs"] = "student-lvm:vm-901001-disk-0,size=4G"
    assert subject.status(operation_id)["state"] == "SUCCEEDED"
    subject.journal.close()


def test_untrusted_fields_and_other_node_rejected_before_receipt(tmp_path):
    node_id = uuid.uuid4()
    subject = dispatcher(tmp_path, node_id, Provider(), [])
    payload = command(node_id)
    payload["spec"]["storage"] = "local-lvm"
    with pytest.raises(CommandError, match="PROFILE_NOT_ALLOWED"):
        subject.submit(encode(payload))
    payload["spec"]["storage"] = "student-lvm"
    payload["node_id"] = str(uuid.uuid4())
    with pytest.raises(CommandError, match="NODE_IDENTITY_MISMATCH"):
        subject.submit(encode(payload))
    with pytest.raises(CommandError, match="INVALID_COMMAND"):
        subject.submit(b'{"kind":"LXC_START","kind":"LXC_SHUTDOWN"}')
    assert subject.journal.get(uuid.UUID(payload["operation_id"])) is None
    subject.journal.close()


def test_bridge_check_requires_local_alias_and_down_state(monkeypatch):
    segment = SegmentSpec.parse(
        {"allocation_id": str(uuid.uuid4()), "mode": "ISOLATED", "cidr": "10.70.0.0/24"}
    )

    class Result:
        stdout = json.dumps(
            [
                {
                    "ifname": segment.bridge,
                    "ifalias": segment.alias,
                    "flags": ["BROADCAST"],
                    "linkinfo": {"info_kind": "bridge"},
                }
            ]
        )

    monkeypatch.setattr("lab_node_agent.command_dispatch.subprocess.run", lambda *a, **k: Result())
    inspect_lab_bridge(segment)

    Result.stdout = json.dumps(
        [
            {
                "ifname": segment.bridge,
                "ifalias": segment.alias,
                "flags": ["UP"],
                "linkinfo": {"info_kind": "bridge"},
            }
        ]
    )
    with pytest.raises(Exception, match="LAB_BRIDGE_NOT_ISOLATED"):
        inspect_lab_bridge(segment)
