"""Local lifecycle for empty Lab Manager bridges; never attaches guest ports."""

import argparse
import hashlib
import ipaddress
import json
import os
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from lab_node_agent.network_policy import SegmentMode

STATE_DIR = Path("/var/lib/lab-manager-node/segments")
PRIVATE_POOLS = tuple(
    ipaddress.ip_network(cidr) for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


class SegmentError(Exception):
    pass


@dataclass(frozen=True)
class SegmentSpec:
    allocation_id: uuid.UUID
    mode: SegmentMode
    cidr: str

    @classmethod
    def parse(cls, value: object) -> "SegmentSpec":
        if not isinstance(value, dict) or set(value) != {"allocation_id", "mode", "cidr"}:
            raise SegmentError("INVALID_SEGMENT_SPEC")
        try:
            allocation_id = uuid.UUID(value["allocation_id"])
            mode = SegmentMode(value["mode"])
            network = ipaddress.ip_network(value["cidr"], strict=True)
        except (TypeError, ValueError, KeyError) as error:
            raise SegmentError("INVALID_SEGMENT_SPEC") from error
        if allocation_id.int == 0:
            raise SegmentError("INVALID_SEGMENT_SPEC")
        if (
            not isinstance(network, ipaddress.IPv4Network)
            or not any(network.subnet_of(pool) for pool in PRIVATE_POOLS)
            or not 16 <= network.prefixlen <= 30
        ):
            raise SegmentError("INVALID_SEGMENT_CIDR")
        return cls(allocation_id, mode, str(network))

    @property
    def bridge(self) -> str:
        value = int.from_bytes(hashlib.sha256(self.allocation_id.bytes).digest()[:8], "big")
        value %= 36**6
        digits = "0123456789abcdefghijklmnopqrstuvwxyz"
        suffix = ""
        for _ in range(6):
            value, digit = divmod(value, 36)
            suffix = digits[digit] + suffix
        return "lmbr" + suffix

    @property
    def alias(self) -> str:
        return "lab-manager:" + str(self.allocation_id)

    def record(self) -> dict[str, str]:
        return {
            "allocation_id": str(self.allocation_id),
            "mode": self.mode.value,
            "cidr": self.cidr,
            "bridge": self.bridge,
        }


def ip(*args: str) -> str:
    result = subprocess.run(["/usr/sbin/ip", *args], capture_output=True, text=True, check=False)
    if result.returncode:
        raise SegmentError("IP_COMMAND_FAILED")
    return result.stdout


class SegmentManager:
    def __init__(self, state_dir: Path = STATE_DIR, *, command=ip, link_exists=None):
        self.state_dir = state_dir
        self.command = command
        self.link_exists = link_exists or (lambda name: (Path("/sys/class/net") / name).exists())
        self.state_file = state_dir / "segments.json"

    def read(self) -> dict[str, dict[str, str]]:
        if self.state_file.is_symlink():
            raise SegmentError("STATE_SYMLINK")
        if not self.state_file.exists():
            return {}
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            if (
                not isinstance(data, dict)
                or type(data.get("version")) is not int
                or data["version"] != 1
            ):
                raise ValueError("version")
            records = data["segments"]
            if not isinstance(records, dict):
                raise ValueError("segments")
            names: set[str] = set()
            for key, record in records.items():
                if not isinstance(record, dict) or set(record) != {
                    "allocation_id",
                    "mode",
                    "cidr",
                    "bridge",
                }:
                    raise ValueError("record")
                spec = SegmentSpec.parse(
                    {field: record[field] for field in ("allocation_id", "mode", "cidr")}
                )
                if str(spec.allocation_id) != key or record.get("bridge") != spec.bridge:
                    raise ValueError("record")
                if spec.bridge in names:
                    raise ValueError("duplicate bridge")
                names.add(spec.bridge)
            return records
        except (OSError, ValueError, KeyError, SegmentError) as error:
            raise SegmentError("INVALID_SEGMENT_STATE") from error

    def write(self, records: dict[str, dict[str, str]]) -> None:
        descriptor, name = tempfile.mkstemp(prefix="segments.", suffix=".tmp", dir=self.state_dir)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump({"version": 1, "segments": records}, stream, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.state_file)
            if os.name == "posix":
                directory = os.open(self.state_dir, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def link(self, spec: SegmentSpec) -> dict | None:
        if not self.link_exists(spec.bridge):
            return None
        try:
            data = json.loads(self.command("-j", "-d", "link", "show", "dev", spec.bridge))
        except ValueError as error:
            raise SegmentError("BRIDGE_INSPECTION_FAILED") from error
        if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
            raise SegmentError("BRIDGE_INSPECTION_FAILED")
        info = data[0].get("linkinfo")
        if not isinstance(info, dict) or info.get("info_kind") != "bridge":
            raise SegmentError("BRIDGE_KIND_MISMATCH")
        if data[0].get("ifalias") != spec.alias:
            raise SegmentError("BRIDGE_OWNER_MISMATCH")
        return data[0]

    def create(self, spec: SegmentSpec) -> str:
        records = self.read()
        key = str(spec.allocation_id)
        if any(
            record["bridge"] == spec.bridge and allocation != key
            for allocation, record in records.items()
        ):
            raise SegmentError("BRIDGE_NAME_COLLISION")
        current = records.get(key)
        if current is not None and current != spec.record():
            raise SegmentError("SEGMENT_SPEC_CONFLICT")
        if current is None:
            if self.link_exists(spec.bridge):
                raise SegmentError("BRIDGE_ALREADY_EXISTS")
            records[key] = spec.record()
            self.write(records)
        if self.link_exists(spec.bridge):
            self.link(spec)
            return spec.bridge
        self.command("link", "add", "name", spec.bridge, "type", "bridge")
        self.command("link", "set", "dev", spec.bridge, "alias", spec.alias)
        self.link(spec)
        return spec.bridge

    def delete(self, allocation_id: uuid.UUID) -> bool:
        records = self.read()
        key = str(allocation_id)
        record = records.get(key)
        if record is None:
            return False
        spec = SegmentSpec.parse(
            {field: record[field] for field in ("allocation_id", "mode", "cidr")}
        )
        link = self.link(spec)
        if link is not None:
            if not isinstance(link.get("flags"), list):
                raise SegmentError("BRIDGE_INSPECTION_FAILED")
            if "UP" in link["flags"]:
                raise SegmentError("BRIDGE_STILL_UP")
            try:
                ports = json.loads(self.command("-j", "link", "show", "master", spec.bridge))
                addresses = json.loads(self.command("-j", "address", "show", "dev", spec.bridge))
            except ValueError as error:
                raise SegmentError("BRIDGE_INSPECTION_FAILED") from error
            if (
                not isinstance(ports, list)
                or not isinstance(addresses, list)
                or len(addresses) != 1
                or not isinstance(addresses[0], dict)
                or not isinstance(addresses[0].get("addr_info"), list)
            ):
                raise SegmentError("BRIDGE_INSPECTION_FAILED")
            if ports or addresses[0]["addr_info"]:
                raise SegmentError("BRIDGE_IN_USE")
            self.command("link", "delete", "dev", spec.bridge, "type", "bridge")
        del records[key]
        self.write(records)
        return True


def main() -> int:
    import fcntl

    parser = argparse.ArgumentParser(description="Manage empty, down Lab Manager bridges locally")
    subcommands = parser.add_subparsers(dest="action", required=True)
    subcommands.add_parser("create").add_argument("spec", type=Path)
    subcommands.add_parser("delete").add_argument("allocation_id")
    subcommands.add_parser("list")
    args = parser.parse_args()
    if os.geteuid() != 0:
        print("ROOT_REQUIRED", file=sys.stderr)
        return 1
    try:
        parent = STATE_DIR.parent
        if parent.is_symlink() or parent.stat().st_uid != 0 or parent.stat().st_mode & 0o002:
            raise SegmentError("STATE_PARENT_UNSAFE")
        STATE_DIR.mkdir(mode=0o700, exist_ok=True)
        if (
            STATE_DIR.is_symlink()
            or STATE_DIR.stat().st_uid != 0
            or STATE_DIR.stat().st_mode & 0o077
        ):
            raise SegmentError("STATE_DIRECTORY_UNSAFE")
        with (STATE_DIR / "segments.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            manager = SegmentManager()
            if args.action == "create":
                spec = SegmentSpec.parse(json.loads(args.spec.read_text(encoding="utf-8")))
                print(manager.create(spec))
            elif args.action == "delete":
                print("DELETED" if manager.delete(uuid.UUID(args.allocation_id)) else "ABSENT")
            else:
                print(json.dumps(manager.read(), sort_keys=True))
    except (OSError, ValueError, SegmentError) as error:
        print(f"Segment operation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
