"""Typed, journaled LXC submission. A receipt is committed before provider I/O."""

import hashlib
import json
import re
import subprocess
import uuid

from lab_node_agent.command_journal import CommandJournal, JournalError
from lab_node_agent.lxc_provider import CreateLxc, LxcOperationError
from lab_node_agent.segments import SegmentError, SegmentSpec


class CommandError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def inspect_lab_bridge(spec: SegmentSpec) -> None:
    """Require an operator-created, still-down bridge before first attachment."""
    try:
        output = subprocess.run(
            ["/usr/sbin/ip", "-j", "-d", "link", "show", "dev", spec.bridge],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
        links = json.loads(output)
    except (OSError, subprocess.SubprocessError, ValueError):
        raise SegmentError("LAB_BRIDGE_UNAVAILABLE") from None
    if not isinstance(links, list) or len(links) != 1 or not isinstance(links[0], dict):
        raise SegmentError("LAB_BRIDGE_UNAVAILABLE")
    link = links[0]
    linkinfo = link.get("linkinfo")
    if (
        link.get("ifname") != spec.bridge
        or link.get("ifalias") != spec.alias
        or not isinstance(linkinfo, dict)
        or linkinfo.get("info_kind") != "bridge"
        or "UP" in link.get("flags", [])
        or link.get("master")
    ):
        raise SegmentError("LAB_BRIDGE_NOT_ISOLATED")


class CommandDispatcher:
    def __init__(
        self,
        *,
        node_id: uuid.UUID,
        template: str,
        storage: str,
        journal: CommandJournal,
        provider,
        inspect_bridge,
    ):
        self.node_id = node_id
        self.template = template
        self.storage = storage
        self.journal = journal
        self.provider = provider
        self.inspect_bridge = inspect_bridge

    def submit(self, body: bytes) -> dict:
        if len(body) > 16384:
            raise CommandError("COMMAND_TOO_LARGE")
        try:
            payload = json.loads(body, object_pairs_hook=unique_object)
            if not isinstance(payload, dict):
                raise ValueError("object required")
            kind = payload["kind"]
            common = {"operation_id", "node_id", "kind", "runtime_id", "generation", "vmid"}
            if kind == "LXC_CREATE":
                if set(payload) != common | {"spec"}:
                    raise ValueError("fields")
            elif kind in ("LXC_START", "LXC_SHUTDOWN"):
                if set(payload) != common:
                    raise ValueError("fields")
            else:
                raise ValueError("kind")
            operation_id = uuid.UUID(payload["operation_id"])
            node_id = uuid.UUID(payload["node_id"])
            runtime_id = uuid.UUID(payload["runtime_id"])
            generation = payload["generation"]
            vmid = payload["vmid"]
            if node_id != self.node_id:
                raise CommandError("NODE_IDENTITY_MISMATCH")
            spec = self._create_spec(payload) if kind == "LXC_CREATE" else None
            expected = (
                {
                    "bridge": spec.bridge,
                    "storage": spec.storage,
                    "disk_gib": spec.disk_gib,
                    "memory_mib": spec.memory_mib,
                    "cores": spec.cores,
                    "hostname": spec.hostname,
                }
                if spec is not None
                else None
            )
            canonical = json.dumps(
                payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            )
            digest = hashlib.sha256(canonical.encode()).hexdigest()
            receipt, created = self.journal.begin(
                operation_id, digest, kind, vmid, runtime_id, generation, expected
            )
        except CommandError:
            raise
        except JournalError as error:
            code = str(error)
            if code in ("OPERATION_ID_CONFLICT", "VMID_OPERATION_IN_PROGRESS"):
                raise CommandError(code) from None
            raise CommandError("INVALID_COMMAND") from None
        except (KeyError, TypeError, ValueError, LxcOperationError, SegmentError):
            raise CommandError("INVALID_COMMAND") from None
        if not created:
            return self._view(receipt)
        try:
            if kind == "LXC_CREATE":
                try:
                    self.inspect_bridge(SegmentSpec.parse(payload["spec"]["segment"]))
                except SegmentError as error:
                    receipt = self.journal.transition(
                        operation_id,
                        from_state="INTENT",
                        to_state="FAILED",
                        error_code=str(error),
                    )
                    return self._view(receipt)
                task = self.provider.create(spec)
            elif kind == "LXC_START":
                task = self.provider.start(vmid, runtime_id, generation)
            else:
                task = self.provider.shutdown(vmid, runtime_id, generation)
            if task is None:
                # A matching guest is not proof that an earlier task completed.
                receipt = self.journal.transition(
                    operation_id,
                    from_state="INTENT",
                    to_state="UNCERTAIN",
                    error_code="PROVIDER_RECONCILIATION_REQUIRED",
                )
            else:
                receipt = self.journal.transition(
                    operation_id, from_state="INTENT", to_state="SUBMITTED", task_id=task
                )
        except (LxcOperationError, SegmentError, OSError) as error:
            # An error after INTENT can occur after Proxmox has accepted the
            # command. Never repeat it merely because the response was lost.
            code = str(error)
            if not code.isupper() or len(code) > 64:
                code = "NODE_COMMAND_UNCERTAIN"
            receipt = self.journal.transition(
                operation_id, from_state="INTENT", to_state="UNCERTAIN", error_code=code
            )
        return self._view(receipt)

    def status(self, operation_id: uuid.UUID) -> dict:
        try:
            receipt = self.journal.get(operation_id)
        except JournalError:
            raise CommandError("INVALID_OPERATION_ID") from None
        if receipt is None:
            raise CommandError("OPERATION_NOT_FOUND")
        task_status = None
        if receipt.state == "SUBMITTED":
            try:
                task = self.provider.task_status(receipt.task_id)
                task_status = task["status"]
                if task["status"] == "stopped":
                    task_status = task.get("exitstatus") or "UNKNOWN"
                    if task_status == "OK" and self._reconciled(receipt):
                        receipt = self.journal.transition(
                            operation_id, from_state="SUBMITTED", to_state="SUCCEEDED"
                        )
                    elif task_status != "OK":
                        receipt = self.journal.transition(
                            operation_id,
                            from_state="SUBMITTED",
                            to_state="UNCERTAIN",
                            error_code="PROXMOX_TASK_FAILED",
                        )
            except LxcOperationError:
                # A missing task or transport failure needs explicit review.
                pass
        return {**self._view(receipt), "task_status": task_status}

    def _reconciled(self, receipt) -> bool:
        config = self.provider.config_for(receipt.vmid)
        if config is None:
            return False
        marker = f"lab-manager:runtime={receipt.runtime_id};generation={receipt.generation}"
        net = str(config.get("net0", "")).split(",")
        if (
            str(config.get("description", "")).rstrip("\r\n") != marker
            or str(config.get("unprivileged")) != "1"
            or str(config.get("onboot", "0")) != "0"
            or "firewall=1" not in net
            or not any(re.fullmatch(r"bridge=lmbr[a-z0-9]{6}", item) for item in net)
        ):
            return False
        status = self.provider.current_status(receipt.vmid)
        if receipt.kind == "LXC_CREATE":
            expected = receipt.expected
            if expected is None:
                return False
            rootfs = str(config.get("rootfs", ""))
            return (
                status == "stopped"
                and str(config.get("ostype")) == "debian"
                and str(config.get("hostname")) == expected["hostname"]
                and str(config.get("memory")) == str(expected["memory_mib"])
                and str(config.get("cores")) == str(expected["cores"])
                and f"bridge={expected['bridge']}" in net
                and "link_down=1" in net
                and "ip=manual" in net
                and "ip6=manual" in net
                and re.fullmatch(
                    rf"{re.escape(expected['storage'])}:vm-{receipt.vmid}-disk-[0-9]+,"
                    rf"size={expected['disk_gib']}G",
                    rootfs,
                )
                is not None
            )
        if receipt.kind == "LXC_START":
            return status == "running"
        return status == "stopped"

    def _create_spec(self, payload: dict) -> CreateLxc:
        raw = payload["spec"]
        if not isinstance(raw, dict) or set(raw) != {
            "hostname",
            "template",
            "storage",
            "segment",
            "memory_mib",
            "cores",
            "disk_gib",
            "ssh_public_key",
        }:
            raise ValueError("spec fields")
        if raw["template"] != self.template or raw["storage"] != self.storage:
            raise CommandError("PROFILE_NOT_ALLOWED")
        segment = SegmentSpec.parse(raw["segment"])
        return CreateLxc(
            runtime_id=uuid.UUID(payload["runtime_id"]),
            generation=payload["generation"],
            vmid=payload["vmid"],
            hostname=raw["hostname"],
            template=raw["template"],
            storage=raw["storage"],
            bridge=segment.bridge,
            memory_mib=raw["memory_mib"],
            cores=raw["cores"],
            disk_gib=raw["disk_gib"],
            ssh_public_key=raw["ssh_public_key"],
        )

    @staticmethod
    def _view(receipt) -> dict:
        return {
            "operation_id": str(receipt.operation_id),
            "kind": receipt.kind,
            "vmid": receipt.vmid,
            "runtime_id": str(receipt.runtime_id),
            "generation": receipt.generation,
            "state": receipt.state,
            "task_id": receipt.task_id,
            "error_code": receipt.error_code,
        }
