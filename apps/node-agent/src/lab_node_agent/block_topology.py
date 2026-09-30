"""Correlate LVM PV paths with current kernel block-device topology.

This is diagnostic evidence only. Device names may change after a reboot, and
neither topology nor absence of a root mount proves disk health or admission.
"""

import json
import re

MAX_INPUT = 2 * 1024 * 1024
MAX_NODES = 10000
MAX_DEPTH = 32
DEVICE_PATH = re.compile(r"/dev/[A-Za-z0-9_.+/:=-]{1,255}")


class TopologyError(Exception):
    pass


def device_path(value):
    if (
        not isinstance(value, str)
        or not DEVICE_PATH.fullmatch(value)
        or any(part in ("", ".", "..") for part in value[5:].split("/"))
    ):
        raise TopologyError("BLOCK_PATH_INVALID")
    return value


def read_json(raw, key):
    if len(raw) > MAX_INPUT:
        raise TopologyError("BLOCK_REPORT_TOO_LARGE")
    try:
        value = json.loads(raw)
        rows = value[key]
    except (KeyError, TypeError, ValueError) as error:
        raise TopologyError("BLOCK_REPORT_INVALID") from error
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_NODES:
        raise TopologyError("BLOCK_REPORT_INVALID")
    return rows


def block_devices(raw):
    """Index paths by all physical disk ancestors, tolerating repeated LVM nodes."""
    rows = read_json(raw, "blockdevices")
    by_path = {}
    visited = 0

    def walk(item, parents, depth):
        nonlocal visited
        visited += 1
        if visited > MAX_NODES or depth > MAX_DEPTH or not isinstance(item, dict):
            raise TopologyError("BLOCK_TREE_INVALID")
        path = device_path(item.get("path"))
        kind = item.get("type")
        size = item.get("size")
        mounts = item.get("mountpoints")
        children = item.get("children", [])
        if (
            not isinstance(kind, str)
            or not 1 <= len(kind) <= 32
            or type(size) is not int
            or size <= 0
            or not isinstance(mounts, list)
            or any(mount is not None and not isinstance(mount, str) for mount in mounts)
            or not isinstance(children, list)
        ):
            raise TopologyError("BLOCK_TREE_INVALID")
        disks = {path} if kind == "disk" else parents
        existing = by_path.get(path)
        if existing is None:
            by_path[path] = {
                "type": kind,
                "size_bytes": size,
                "mountpoints": mounts,
                "disks": set(disks),
            }
        elif (
            existing["type"] != kind
            or existing["size_bytes"] != size
            or existing["mountpoints"] != mounts
        ):
            raise TopologyError("BLOCK_PATH_CONFLICT")
        else:
            existing["disks"].update(disks)
        for child in children:
            walk(child, disks, depth + 1)

    for row in rows:
        walk(row, set(), 0)
    return by_path


def root_source(raw):
    rows = read_json(raw, "filesystems")
    if len(rows) != 1 or not isinstance(rows[0], dict) or rows[0].get("target") != "/":
        raise TopologyError("ROOT_MOUNT_INVALID")
    return device_path(rows[0].get("source"))


def add_topology(pools, lsblk_raw, findmnt_raw):
    devices = block_devices(lsblk_raw)
    source = root_source(findmnt_raw)
    root = devices.get(source)
    if root is None or "/" not in root["mountpoints"] or not root["disks"]:
        raise TopologyError("ROOT_DEVICE_UNKNOWN")
    for pool in pools:
        for volume in pool["backing"]["physical_volumes"]:
            path = volume["name"]
            item = devices.get(path)
            if item is None or not item["disks"] or item["size_bytes"] < volume["size_bytes"]:
                raise TopologyError("PV_BLOCK_DEVICE_UNKNOWN")
            volume["topology"] = {
                "type": item["type"],
                "size_bytes": item["size_bytes"],
                "backing_disks": sorted(item["disks"]),
                "whole_disk": item["type"] == "disk" and item["disks"] == {path},
                "shares_system_disk": bool(item["disks"] & root["disks"]),
            }
    return pools
