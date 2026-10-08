"""Render lab network rules without changing host networking."""

import argparse
import ipaddress
import json
import re
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

_BRIDGE_NAME = re.compile(r"lmbr[a-z0-9]{1,6}\Z")
_GUEST_MAC = re.compile(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\Z")


class SegmentMode(StrEnum):
    ISOLATED = "ISOLATED"
    GROUP_LAN = "GROUP_LAN"


@dataclass(frozen=True)
class Segment:
    bridge: str
    mode: SegmentMode


@dataclass(frozen=True)
class IsolatedSshGuest:
    """One owned veth on an isolated /30, reachable only from Guacamole."""

    bridge: str
    vmid: int
    cidr: str
    address: str
    mac: str

    @property
    def port(self) -> str:
        return f"veth{self.vmid}i0"

    def validate(self) -> None:
        try:
            subnet = ipaddress.IPv4Network(self.cidr, strict=True)
            address = ipaddress.IPv4Address(self.address)
        except (TypeError, ValueError) as error:
            raise ValueError("INVALID_GUEST_ADDRESS") from error
        if (
            not isinstance(self.bridge, str)
            or not _BRIDGE_NAME.fullmatch(self.bridge)
            or type(self.vmid) is not int
            or not 100 <= self.vmid <= 999999999
            or subnet.prefixlen != 30
            or not subnet.subnet_of(ipaddress.IPv4Network("10.70.0.0/16"))
            or address != subnet.network_address + 2
            or not isinstance(self.mac, str)
            or not _GUEST_MAC.fullmatch(self.mac)
            or int(self.mac[:2], 16) & 1
        ):
            raise ValueError("INVALID_ISOLATED_SSH_GUEST")


def render_isolated_ssh_policy(
    segments: tuple[Segment, ...],
    guests: tuple[IsolatedSshGuest, ...],
    *,
    guacamole_ipv4: str,
    guacamole_bridge: str,
) -> str:
    """Grant one SSH path per owned /30, while keeping all other lab paths denied.

    This renders rules only. A root-owned helper must compare each guest with
    the live Proxmox config and atomically apply the complete nftables file.
    """
    try:
        source = ipaddress.IPv4Address(guacamole_ipv4)
    except (TypeError, ValueError) as error:
        raise ValueError("INVALID_GUACAMOLE_SOURCE") from error
    if (
        not source.is_private
        or source in ipaddress.IPv4Network("10.70.0.0/16")
        or not isinstance(guacamole_bridge, str)
        or not re.fullmatch(r"[a-z][a-z0-9]{1,14}", guacamole_bridge)
        or guacamole_bridge.startswith("lmbr")
    ):
        raise ValueError("INVALID_GUACAMOLE_SOURCE")
    modes = {segment.bridge: segment.mode for segment in segments}
    if len(modes) != len(segments):
        raise ValueError("DUPLICATE_BRIDGE_NAME")
    bridges: set[str] = set()
    ports: set[str] = set()
    for guest in guests:
        if not isinstance(guest, IsolatedSshGuest):
            raise ValueError("INVALID_ISOLATED_SSH_GUEST")
        guest.validate()
        if modes.get(guest.bridge) is not SegmentMode.ISOLATED:
            raise ValueError("ISOLATED_SEGMENT_REQUIRED")
        if guest.bridge in bridges or guest.port in ports:
            raise ValueError("DUPLICATE_ISOLATED_GUEST")
        bridges.add(guest.bridge)
        ports.add(guest.port)

    rules = render_l2_policy(segments)
    forward = []
    bridge_input = []
    for guest in sorted(guests, key=lambda item: item.bridge):
        address = ipaddress.IPv4Address(guest.address)
        gateway = ipaddress.IPv4Network(guest.cidr).network_address + 1
        mac = guest.mac.lower()
        forward.extend(
            [
                f'        iifname "{guacamole_bridge}" ip saddr {source} '
                f'oifname "{guest.bridge}" ip daddr {address} '
                "tcp dport 22 ct state new,established accept\n",
                f'        iifname "{guest.bridge}" ip saddr {address} '
                f'oifname "{guacamole_bridge}" ip daddr {source} '
                "tcp sport 22 ct state established,related accept\n",
            ]
        )
        bridge_input.extend(
            [
                f'        meta ibrname "{guest.bridge}" iifname "{guest.port}" '
                f"ether saddr {mac} arp saddr ip {address} "
                f"arp daddr ip {gateway} accept\n",
                f'        meta ibrname "{guest.bridge}" iifname "{guest.port}" '
                f"ether saddr {mac} ip saddr {address} accept\n",
            ]
        )
    forward_drop = '        iifname "lmbr*" drop\n        oifname "lmbr*" drop\n'
    bridge_input_drop = '        meta ibrname "lmbr*" drop\n'
    if rules.count(forward_drop) != 1 or rules.count(bridge_input_drop) != 2:
        raise ValueError("BASE_POLICY_CHANGED")
    rules = rules.replace(forward_drop, "".join(forward) + forward_drop, 1)
    return rules.replace(bridge_input_drop, "".join(bridge_input) + bridge_input_drop, 1)


def render_l2_policy(segments: tuple[Segment, ...]) -> str:
    """Keep the base guard; open only L2 forwarding on named group bridges.

    This is intentionally not an admission policy. Host access, routed traffic,
    Internet access and Guacamole remain denied for every lab bridge.
    """
    names: set[str] = set()
    for segment in segments:
        if not isinstance(segment, Segment):
            raise ValueError("INVALID_SEGMENT")
        if not isinstance(segment.bridge, str) or not _BRIDGE_NAME.fullmatch(segment.bridge):
            raise ValueError("INVALID_BRIDGE_NAME")
        if segment.bridge in names:
            raise ValueError("DUPLICATE_BRIDGE_NAME")
        names.add(segment.bridge)
        if not isinstance(segment.mode, SegmentMode):
            raise ValueError("INVALID_SEGMENT_MODE")

    permits = "".join(
        f'        meta ibrname "{segment.bridge}" accept\n'
        for segment in sorted(segments, key=lambda item: item.bridge)
        if segment.mode is SegmentMode.GROUP_LAN
    )
    return (
        "# Generated Lab Manager L2 policy. No host or routed access is granted.\n"
        "destroy table inet lab_manager\n"
        "destroy table bridge lab_manager_l2\n"
        "table inet lab_manager {\n"
        "    chain guest_input {\n"
        "        type filter hook input priority -10; policy accept;\n"
        '        iifname "lmbr*" drop\n'
        "    }\n"
        "    chain guest_forward {\n"
        "        type filter hook forward priority -10; policy accept;\n"
        '        iifname "lmbr*" drop\n'
        '        oifname "lmbr*" drop\n'
        "    }\n"
        "    chain guest_output {\n"
        "        type filter hook output priority -10; policy accept;\n"
        '        oifname "lmbr*" drop\n'
        "    }\n"
        "}\n"
        "table bridge lab_manager_l2 {\n"
        "    chain guest_bridge_input {\n"
        "        type filter hook input priority -10; policy accept;\n"
        '        meta ibrname "lmbr*" drop\n'
        "    }\n"
        "    chain guest_bridge_forward {\n"
        "        type filter hook forward priority -10; policy accept;\n"
        f"{permits}"
        '        meta ibrname "lmbr*" drop\n'
        "    }\n"
        "}\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Render lab L2 nftables policy; do not apply it")
    parser.add_argument("config", type=Path, help="Validated lab network policy JSON")
    args = parser.parse_args()
    try:
        data = json.loads(args.config.read_text(encoding="utf-8"))
        if isinstance(data, list):
            segment_data = data
            ssh_data = None
        elif isinstance(data, dict) and set(data) == {"segments", "guacamole", "guests"}:
            segment_data = data["segments"]
            ssh_data = data
        else:
            raise ValueError("INVALID_NETWORK_POLICY")
        if not isinstance(segment_data, list):
            raise ValueError("INVALID_SEGMENT_LIST")
        segments = []
        for item in segment_data:
            if not isinstance(item, dict) or set(item) != {"bridge", "mode"}:
                raise ValueError("INVALID_SEGMENT")
            segments.append(Segment(item["bridge"], SegmentMode(item["mode"])))
        if ssh_data is None:
            rules = render_l2_policy(tuple(segments))
        else:
            guacamole = ssh_data["guacamole"]
            guest_data = ssh_data["guests"]
            if (
                not isinstance(guacamole, dict)
                or set(guacamole) != {"address", "bridge"}
                or not isinstance(guest_data, list)
            ):
                raise ValueError("INVALID_SSH_POLICY")
            guests = []
            for item in guest_data:
                if not isinstance(item, dict) or set(item) != {
                    "bridge",
                    "vmid",
                    "cidr",
                    "address",
                    "mac",
                }:
                    raise ValueError("INVALID_SSH_GUEST")
                guests.append(IsolatedSshGuest(**item))
            rules = render_isolated_ssh_policy(
                tuple(segments),
                tuple(guests),
                guacamole_ipv4=guacamole["address"],
                guacamole_bridge=guacamole["bridge"],
            )
        sys.stdout.write(rules)
    except (OSError, ValueError, TypeError) as error:
        print(f"Policy render failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
