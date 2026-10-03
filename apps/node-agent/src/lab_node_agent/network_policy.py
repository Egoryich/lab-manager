"""Render the L2 part of a lab policy without changing host networking."""

import argparse
import json
import re
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

_BRIDGE_NAME = re.compile(r"lmbr[a-z0-9]{1,11}\Z")


class SegmentMode(StrEnum):
    ISOLATED = "ISOLATED"
    GROUP_LAN = "GROUP_LAN"


@dataclass(frozen=True)
class Segment:
    bridge: str
    mode: SegmentMode


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
    parser.add_argument("config", type=Path, help="JSON array of {bridge, mode} entries")
    args = parser.parse_args()
    try:
        data = json.loads(args.config.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("INVALID_SEGMENT_LIST")
        segments = []
        for item in data:
            if not isinstance(item, dict) or set(item) != {"bridge", "mode"}:
                raise ValueError("INVALID_SEGMENT")
            segments.append(Segment(item["bridge"], SegmentMode(item["mode"])))
        sys.stdout.write(render_l2_policy(tuple(segments)))
    except (OSError, ValueError, TypeError) as error:
        print(f"Policy render failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
