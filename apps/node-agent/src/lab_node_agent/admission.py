"""Root-owned, fail-closed SSH admission for one isolated LXC per segment."""

import ipaddress
import json
import os
import re
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from lab_node_agent.network_policy import (
    IsolatedSshGuest,
    Segment,
    SegmentMode,
    render_isolated_ssh_policy,
    render_l2_policy,
)
from lab_node_agent.segments import STATE_DIR, SegmentError, SegmentManager, SegmentSpec

ADMISSIONS = STATE_DIR / "admissions.json"
GUACAMOLE_SOURCE = Path("/etc/lab-manager-node/guacamole-source.json")
LXC_CONFIG_DIR = Path("/etc/pve/lxc")
_MAC = re.compile(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\Z")


@dataclass(frozen=True)
class AdmissionRequest:
    allocation_id: uuid.UUID
    runtime_id: uuid.UUID
    generation: int
    vmid: int
    address: str

    @classmethod
    def parse(cls, value: object) -> "AdmissionRequest":
        if not isinstance(value, dict) or set(value) != {
            "allocation_id",
            "runtime_id",
            "generation",
            "vmid",
            "address",
        }:
            raise SegmentError("INVALID_SSH_ADMISSION")
        try:
            request = cls(
                uuid.UUID(value["allocation_id"]),
                uuid.UUID(value["runtime_id"]),
                value["generation"],
                value["vmid"],
                str(ipaddress.IPv4Address(value["address"])),
            )
        except (TypeError, ValueError, AttributeError) as error:
            raise SegmentError("INVALID_SSH_ADMISSION") from error
        if (
            request.allocation_id.int == 0
            or request.runtime_id.int == 0
            or type(request.generation) is not int
            or not 1 <= request.generation <= 1000000
            or type(request.vmid) is not int
            or not 100 <= request.vmid <= 999999999
        ):
            raise SegmentError("INVALID_SSH_ADMISSION")
        return request

    def record(self) -> dict:
        return {
            "allocation_id": str(self.allocation_id),
            "runtime_id": str(self.runtime_id),
            "generation": self.generation,
            "vmid": self.vmid,
            "address": self.address,
        }


@dataclass(frozen=True)
class Admission:
    allocation_id: uuid.UUID
    runtime_id: uuid.UUID
    generation: int
    vmid: int
    address: str
    mac: str

    @classmethod
    def parse(cls, value: object) -> "Admission":
        if not isinstance(value, dict) or set(value) != {
            "allocation_id",
            "runtime_id",
            "generation",
            "vmid",
            "address",
            "mac",
        }:
            raise SegmentError("INVALID_SSH_ADMISSION")
        try:
            admission = cls(
                uuid.UUID(value["allocation_id"]),
                uuid.UUID(value["runtime_id"]),
                value["generation"],
                value["vmid"],
                str(ipaddress.IPv4Address(value["address"])),
                value["mac"].lower(),
            )
        except (TypeError, ValueError, AttributeError) as error:
            raise SegmentError("INVALID_SSH_ADMISSION") from error
        if (
            admission.allocation_id.int == 0
            or admission.runtime_id.int == 0
            or type(admission.generation) is not int
            or not 1 <= admission.generation <= 1000000
            or type(admission.vmid) is not int
            or not 100 <= admission.vmid <= 999999999
            or not isinstance(admission.mac, str)
            or not _MAC.fullmatch(admission.mac)
            or int(admission.mac[:2], 16) & 1
        ):
            raise SegmentError("INVALID_SSH_ADMISSION")
        return admission

    def record(self) -> dict:
        return {
            "allocation_id": str(self.allocation_id),
            "runtime_id": str(self.runtime_id),
            "generation": self.generation,
            "vmid": self.vmid,
            "address": self.address,
            "mac": self.mac,
        }


def read_guacamole_source(path: Path = GUACAMOLE_SOURCE) -> dict[str, str]:
    if path.is_symlink():
        raise SegmentError("GUACAMOLE_SOURCE_UNSAFE")
    try:
        details = path.stat()
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SegmentError("GUACAMOLE_SOURCE_UNAVAILABLE") from error
    if details.st_uid != 0 or details.st_mode & 0o022:
        raise SegmentError("GUACAMOLE_SOURCE_UNSAFE")
    if not isinstance(value, dict) or set(value) != {"address", "bridge"}:
        raise SegmentError("GUACAMOLE_SOURCE_INVALID")
    try:
        address = ipaddress.IPv4Address(value["address"])
    except (TypeError, ValueError) as error:
        raise SegmentError("GUACAMOLE_SOURCE_INVALID") from error
    if (
        not address.is_private
        or not isinstance(value["bridge"], str)
        or not re.fullmatch(r"[a-z][a-z0-9]{1,14}", value["bridge"])
    ):
        raise SegmentError("GUACAMOLE_SOURCE_INVALID")
    if value["bridge"].startswith("lmbr"):
        raise SegmentError("GUACAMOLE_SOURCE_INVALID")
    return {"address": str(address), "bridge": value["bridge"]}


def network_readiness(
    source=read_guacamole_source,
    run=subprocess.run,
    bridge_root=Path("/sys/class/net"),
    policy=Path("/etc/lab-manager-node/guacamole-egress.nft"),
) -> dict:
    """Read live root-owned prerequisites; any missing check keeps admission closed."""
    try:
        guacamole = source()
        bridge = guacamole["bridge"]
        if not (bridge_root / bridge / "bridge").is_dir():
            return {"ready": False}
        details = policy.stat()
        rules = policy.read_text(encoding="utf-8")
        if (
            not policy.is_file()
            or policy.is_symlink()
            or (os.name == "posix" and (details.st_uid != 0 or details.st_mode & 0o022))
            or f'iifname "{bridge}" ip saddr {guacamole["address"]}' not in rules
        ):
            return {"ready": False}
        checks = (
            ("/usr/bin/systemctl", "is-active", "--quiet", "lab-node-network-guard.service"),
            ("/usr/bin/systemctl", "is-active", "--quiet", "lab-guacamole-egress.service"),
            ("/usr/sbin/nft", "list", "table", "inet", "lab_manager"),
            ("/usr/sbin/nft", "list", "table", "bridge", "lab_manager_l2"),
            ("/usr/sbin/nft", "list", "table", "inet", "lab_guac_filter"),
            ("/usr/sbin/nft", "list", "table", "ip", "lab_guac_nat"),
            ("/usr/sbin/nft", "list", "table", "bridge", "lab_guac_l2"),
        )
        for command in checks:
            if run(command, capture_output=True, timeout=3, check=False).returncode != 0:
                return {"ready": False}
        return {"ready": True, "guacamole": guacamole}
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired, SegmentError):
        return {"ready": False}


def read_lxc_config(vmid: int, directory: Path = LXC_CONFIG_DIR) -> dict[str, str]:
    """Read only the root-owned Proxmox config, without a shell or API token."""
    path = directory / f"{vmid}.conf"
    if path.is_symlink():
        raise SegmentError("GUEST_CONFIG_UNSAFE")
    try:
        contents = path.read_text(encoding="utf-8")
    except OSError as error:
        raise SegmentError("GUEST_CONFIG_UNAVAILABLE") from error
    result = {}
    for line in contents.splitlines():
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition(": ")
        if not sep or key in result:
            raise SegmentError("GUEST_CONFIG_INVALID")
        result[key] = value
    return result


def verify_lxc(admission: Admission, segment: SegmentSpec, config: dict[str, str]) -> None:
    if segment.mode is not SegmentMode.ISOLATED:
        raise SegmentError("ISOLATED_SEGMENT_REQUIRED")
    guest = IsolatedSshGuest(
        segment.bridge, admission.vmid, segment.cidr, admission.address, admission.mac
    )
    try:
        guest.validate()
    except ValueError as error:
        raise SegmentError("GUEST_ADDRESS_MISMATCH") from error
    marker = f"lab-manager:runtime={admission.runtime_id};generation={admission.generation}"
    if (
        config.get("description", "").rstrip("\r\n") != marker
        or config.get("unprivileged") != "1"
        or config.get("onboot", "0") != "0"
        or config.get("ostype") != "debian"
        or any(key.startswith("net") and key != "net0" for key in config)
    ):
        raise SegmentError("GUEST_OWNERSHIP_UNCONFIRMED")
    net = {}
    for field in config.get("net0", "").split(","):
        name, sep, value = field.partition("=")
        if not sep or name in net:
            raise SegmentError("GUEST_NETWORK_DRIFT")
        net[name] = value
    subnet = ipaddress.IPv4Network(segment.cidr)
    expected = {
        "name": "eth0",
        "bridge": segment.bridge,
        "firewall": "1",
        "hwaddr": admission.mac,
        "ip": f"{admission.address}/{subnet.prefixlen}",
        "gw": str(subnet.network_address + 1),
        "ip6": "manual",
        "type": "veth",
    }
    if net.pop("link_down", None) not in (None, "1"):
        raise SegmentError("GUEST_NETWORK_DRIFT")
    if {key: value.lower() if key == "hwaddr" else value for key, value in net.items()} != expected:
        raise SegmentError("GUEST_NETWORK_DRIFT")


def assigned_mac(config: dict[str, str]) -> str:
    """Use the MAC Proxmox assigned, after the full config is checked separately."""
    matches = [
        field[7:] for field in config.get("net0", "").split(",") if field.startswith("hwaddr=")
    ]
    if len(matches) != 1 or not _MAC.fullmatch(matches[0]) or int(matches[0][:2], 16) & 1:
        raise SegmentError("GUEST_NETWORK_DRIFT")
    return matches[0].lower()


def apply_nft(rules: str) -> None:
    """Check then load a complete Lab Manager table transaction."""
    descriptor, path = tempfile.mkstemp(prefix="admission.", suffix=".nft", dir=STATE_DIR)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(rules)
            output.flush()
            os.fsync(output.fileno())
        for arguments in (("--check", "--file", path), ("--file", path)):
            result = subprocess.run(
                ["/usr/sbin/nft", *arguments], capture_output=True, timeout=10, check=False
            )
            if result.returncode:
                raise SegmentError("FIREWALL_APPLY_FAILED")
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SegmentError("FIREWALL_APPLY_FAILED") from error
    finally:
        os.unlink(path)


class AdmissionManager:
    def __init__(
        self,
        segments: SegmentManager,
        *,
        state_file: Path = ADMISSIONS,
        source=read_guacamole_source,
        config=read_lxc_config,
        firewall=apply_nft,
    ):
        self.segments = segments
        self.state_file = state_file
        self.source = source
        self.config = config
        self.firewall = firewall

    def read(self) -> dict[str, Admission]:
        if self.state_file.is_symlink():
            raise SegmentError("ADMISSION_STATE_UNSAFE")
        if not self.state_file.exists():
            return {}
        try:
            value = json.loads(self.state_file.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or set(value) != {"version", "admissions"}:
                raise ValueError("shape")
            if type(value["version"]) is not int or value["version"] != 1:
                raise ValueError("version")
            records = value["admissions"]
            if not isinstance(records, dict):
                raise ValueError("admissions")
            parsed = {key: Admission.parse(item) for key, item in records.items()}
            if any(str(item.allocation_id) != key for key, item in parsed.items()):
                raise ValueError("key")
            if len({item.vmid for item in parsed.values()}) != len(parsed):
                raise ValueError("vmid")
            return parsed
        except (OSError, ValueError, SegmentError) as error:
            raise SegmentError("ADMISSION_STATE_INVALID") from error

    def write(self, records: dict[str, Admission]) -> None:
        directory_path = self.state_file.parent
        descriptor, name = tempfile.mkstemp(prefix="admissions.", suffix=".tmp", dir=directory_path)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(
                    {
                        "version": 1,
                        "admissions": {key: item.record() for key, item in records.items()},
                    },
                    output,
                    sort_keys=True,
                )
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, self.state_file)
            if os.name == "posix":
                directory = os.open(directory_path, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def render(self, records: dict[str, Admission]) -> str:
        if not records:
            return render_l2_policy(())
        segments = self.segments.read()
        source = self.source()
        selected = []
        guests = []
        for allocation_id, admission in records.items():
            record = segments.get(allocation_id)
            if record is None:
                raise SegmentError("SEGMENT_NOT_FOUND")
            segment = SegmentSpec.parse(
                {field: record[field] for field in ("allocation_id", "mode", "cidr")}
            )
            if segment.mode is not SegmentMode.ISOLATED:
                raise SegmentError("ISOLATED_SEGMENT_REQUIRED")
            link = self.segments.link(segment)
            if link is None or "UP" not in link.get("flags", []):
                raise SegmentError("SEGMENT_GATEWAY_NOT_READY")
            addresses = self.segments._address(segment)
            if not addresses:
                raise SegmentError("SEGMENT_GATEWAY_NOT_READY")
            self.segments._check_gateway_address(segment, addresses)
            verify_lxc(admission, segment, self.config(admission.vmid))
            selected.append(Segment(segment.bridge, segment.mode))
            guests.append(
                IsolatedSshGuest(
                    segment.bridge,
                    admission.vmid,
                    segment.cidr,
                    admission.address,
                    admission.mac,
                )
            )
        return render_isolated_ssh_policy(
            tuple(selected),
            tuple(guests),
            guacamole_ipv4=source["address"],
            guacamole_bridge=source["bridge"],
        )

    def restore(self) -> None:
        self.firewall(self.render(self.read()))

    def admit(self, request: AdmissionRequest) -> dict:
        admission = Admission(
            request.allocation_id,
            request.runtime_id,
            request.generation,
            request.vmid,
            request.address,
            assigned_mac(self.config(request.vmid)),
        )
        records = self.read()
        key = str(admission.allocation_id)
        if key in records and records[key] != admission:
            raise SegmentError("SSH_ADMISSION_CONFLICT")
        updated = {**records, key: admission}
        if len({item.vmid for item in updated.values()}) != len(updated):
            raise SegmentError("VMID_ALREADY_ADMITTED")
        rules = self.render(updated)
        self.firewall(render_l2_policy(()))
        self.write(updated)
        self.firewall(rules)
        return admission.record()

    def revoke(self, allocation_id: uuid.UUID) -> bool:
        records = self.read()
        key = str(allocation_id)
        if key not in records:
            return False
        updated = {item_id: item for item_id, item in records.items() if item_id != key}
        self.firewall(render_l2_policy(()))
        self.write(updated)
        self.firewall(self.render(updated))
        return True
