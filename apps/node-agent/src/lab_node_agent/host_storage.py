"""Read-only, local LVM-thin diagnostic for a future reconciled node inventory.

This probe never marks a pool admissible. It is run locally by an administrator;
the mTLS agent does not execute privileged commands or accept probe parameters.
"""

import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from lab_node_agent.block_topology import TopologyError, add_topology

STORAGE_CONFIG = Path("/etc/pve/storage.cfg")
LVS = (
    "lvs",
    "--all",
    "--reportformat",
    "json",
    "--units",
    "b",
    "--nosuffix",
    "-o",
    "vg_name,lv_name,lv_size,data_percent,metadata_percent,lv_attr,pool_lv,origin",
)
PVS = (
    "pvs",
    "--reportformat",
    "json",
    "--units",
    "b",
    "--nosuffix",
    "-o",
    "pv_name,pv_uuid,vg_name,pv_size,pv_free",
)
VGS = (
    "vgs",
    "--reportformat",
    "json",
    "--units",
    "b",
    "--nosuffix",
    "-o",
    "vg_name,vg_uuid,vg_size,vg_free,pv_count",
)
LSBLK = (
    "lsblk",
    "--json",
    "--bytes",
    "--tree",
    "--output",
    "NAME,PATH,TYPE,SIZE,PKNAME,MOUNTPOINTS",
)
FINDMNT = ("findmnt", "--json", "--mountpoint", "/", "--output", "SOURCE,TARGET,FSTYPE")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}")
LV_IDENTIFIER = re.compile(r"\[?[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}\]?")
DEVICE_PATH = re.compile(r"/dev/[A-Za-z0-9_.+/:=-]{1,255}")
LVM_UUID = re.compile(r"[A-Za-z0-9]{6}(?:-[A-Za-z0-9]{4}){5}-[A-Za-z0-9]{6}")
VOLUME_OWNER = re.compile(r"(?:vm|base)-(\d+)-.+")
MAX_INPUT = 2 * 1024 * 1024
SNAPSHOT = Path("/var/lib/lab-manager-node/storage.json")
MAX_SNAPSHOT_AGE = timedelta(seconds=120)


class StorageProbeError(Exception):
    pass


def identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise StorageProbeError("INVALID_LVM_IDENTIFIER")
    return value


def lv_identifier(value):
    if not isinstance(value, str) or not LV_IDENTIFIER.fullmatch(value):
        raise StorageProbeError("INVALID_LVM_IDENTIFIER")
    return value


def device_path(value):
    if (
        not isinstance(value, str)
        or not DEVICE_PATH.fullmatch(value)
        or any(part in ("", ".", "..") for part in value[5:].split("/"))
    ):
        raise StorageProbeError("INVALID_PV_NAME")
    return value


def lvm_uuid(value):
    if not isinstance(value, str) or not LVM_UUID.fullmatch(value):
        raise StorageProbeError("INVALID_LVM_UUID")
    return value


def nonnegative_integer(value):
    try:
        number = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise StorageProbeError("INVALID_LVM_SIZE") from error
    if not number.is_finite() or number != number.to_integral_value() or not 0 <= number < 2**63:
        raise StorageProbeError("INVALID_LVM_SIZE")
    return int(number)


def percent(value):
    try:
        number = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise StorageProbeError("INVALID_LVM_PERCENT") from error
    if not number.is_finite() or not 0 <= number <= 100:
        raise StorageProbeError("INVALID_LVM_PERCENT")
    return float(number)


def storage_pools(config):
    if len(config.encode()) > MAX_INPUT:
        raise StorageProbeError("STORAGE_CONFIG_TOO_LARGE")
    pools = {}
    current = None
    for line in config.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[0].isspace():
            match = re.fullmatch(r"lvmthin:\s+(\S+)\s*", line)
            current = identifier(match.group(1)) if match else None
            if current is not None:
                if current in pools or len(pools) >= 128:
                    raise StorageProbeError("DUPLICATE_OR_EXCESS_STORAGE")
                pools[current] = {}
            continue
        if current is None:
            continue
        match = re.fullmatch(r"\s+(vgname|thinpool)\s+(\S+)\s*", line)
        if match:
            key, value = match.groups()
            if key in pools[current]:
                raise StorageProbeError("DUPLICATE_STORAGE_PROPERTY")
            pools[current][key] = identifier(value)
    if any(set(config) != {"vgname", "thinpool"} for config in pools.values()):
        raise StorageProbeError("INCOMPLETE_STORAGE_CONFIG")
    return pools


def parse_report(raw, section):
    if len(raw) > MAX_INPUT:
        raise StorageProbeError("LVM_REPORT_TOO_LARGE")
    try:
        data = json.loads(raw)
        reports = data["report"]
        if not isinstance(reports, list) or len(reports) != 1:
            raise ValueError("Invalid report count")
        rows = reports[0][section]
        if not isinstance(rows, list) or len(rows) > 10000:
            raise ValueError("Invalid LV count")
        return rows
    except (KeyError, TypeError, ValueError) as error:
        raise StorageProbeError("LVM_REPORT_INVALID") from error


def parse_lvs_report(raw):
    return parse_report(raw, "lv")


def summarize_backing(pools, pv_rows, vg_rows):
    """Correlate reported LVM objects without claiming physical admission."""
    groups = {}
    seen_vg_uuids = set()
    for row in vg_rows:
        name = identifier(row.get("vg_name"))
        if name in groups:
            raise StorageProbeError("DUPLICATE_VG")
        vg_uuid = lvm_uuid(row.get("vg_uuid"))
        if vg_uuid in seen_vg_uuids:
            raise StorageProbeError("DUPLICATE_VG_UUID")
        seen_vg_uuids.add(vg_uuid)
        size = nonnegative_integer(row.get("vg_size"))
        free = nonnegative_integer(row.get("vg_free"))
        count = nonnegative_integer(row.get("pv_count"))
        if size <= 0 or free > size or count < 1:
            raise StorageProbeError("VG_REPORT_INVALID")
        groups[name] = {"uuid": vg_uuid, "size": size, "free": free, "pv_count": count}
    physical = {}
    seen_names = set()
    seen_pv_uuids = set()
    for row in pv_rows:
        name = device_path(row.get("pv_name"))
        pv_uuid = lvm_uuid(row.get("pv_uuid"))
        if name in seen_names:
            raise StorageProbeError("DUPLICATE_PV")
        if pv_uuid in seen_pv_uuids:
            raise StorageProbeError("DUPLICATE_PV_UUID")
        seen_names.add(name)
        seen_pv_uuids.add(pv_uuid)
        vg_raw = row.get("vg_name")
        if not vg_raw:
            continue
        vg = identifier(vg_raw)
        size = nonnegative_integer(row.get("pv_size"))
        free = nonnegative_integer(row.get("pv_free"))
        if size <= 0 or free > size:
            raise StorageProbeError("PV_REPORT_INVALID")
        physical.setdefault(vg, []).append(
            {"name": name, "pv_uuid": pv_uuid, "size_bytes": size, "unallocated_bytes": free}
        )
    for pool in pools:
        vgname = pool["vgname"]
        group = groups.get(vgname)
        devices = sorted(physical.get(vgname, []), key=lambda item: item["name"])
        if (
            group is None
            or len(devices) != group["pv_count"]
            or pool["pool_size_bytes"] > group["size"]
            or sum(item["size_bytes"] for item in devices) < group["size"]
            or sum(item["unallocated_bytes"] for item in devices) != group["free"]
        ):
            raise StorageProbeError("LVM_BACKING_MISMATCH")
        pool["backing"] = {
            "vg_uuid": group["uuid"],
            "vg_size_bytes": group["size"],
            "vg_unallocated_bytes": group["free"],
            "physical_volumes": devices,
            "physical_backing_reconciled": False,
        }
    return pools


def summarize(config, rows):
    pools = storage_pools(config)
    if len(rows) > 10000:
        raise StorageProbeError("LVM_REPORT_INVALID")
    seen = set()
    volumes = {}
    pool_rows = {}
    for row in rows:
        if not isinstance(row, dict):
            raise StorageProbeError("LVM_REPORT_INVALID")
        vg = identifier(row.get("vg_name"))
        lv = lv_identifier(row.get("lv_name"))
        if (vg, lv) in seen:
            raise StorageProbeError("DUPLICATE_LV")
        seen.add((vg, lv))
        attr = row.get("lv_attr")
        if not isinstance(attr, str) or len(attr) < 1:
            raise StorageProbeError("LVM_REPORT_INVALID")
        pool = row.get("pool_lv") or ""
        if attr[0] == "V" and not pool:
            raise StorageProbeError("THIN_VOLUME_POOL_UNKNOWN")
        if pool:
            pool = lv_identifier(pool)
            owner = VOLUME_OWNER.fullmatch(lv)
            volumes.setdefault((vg, pool), []).append(
                {
                    "name": lv,
                    "size_bytes": nonnegative_integer(row.get("lv_size")),
                    "owner_vmid_from_name": int(owner.group(1)) if owner else None,
                    "origin": lv_identifier(row["origin"]) if row.get("origin") else None,
                    "ownership": "UNVERIFIED",
                }
            )
        if attr[0] == "t":
            pool_rows[(vg, lv)] = row
    result = []
    for storage, definition in sorted(pools.items()):
        key = (definition["vgname"], definition["thinpool"])
        pool = pool_rows.get(key)
        if pool is None:
            raise StorageProbeError("THIN_POOL_NOT_FOUND")
        contents = sorted(volumes.get(key, []), key=lambda item: item["name"])
        result.append(
            {
                "storage": storage,
                "vgname": key[0],
                "thinpool": key[1],
                "pool_size_bytes": nonnegative_integer(pool.get("lv_size")),
                "data_percent": percent(pool.get("data_percent")),
                "metadata_percent": percent(pool.get("metadata_percent")),
                "volumes": contents,
                "ownership_reconciled": False,
            }
        )
    return result


def collect_local():
    if getattr(os, "geteuid", lambda: -1)() != 0:
        raise StorageProbeError("ROOT_REQUIRED")
    try:
        config = STORAGE_CONFIG.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise StorageProbeError("LVM_COLLECTION_FAILED") from error
    reports = []
    for command in (LVS, PVS, VGS, LSBLK, FINDMNT):
        failure = (
            "BLOCK_COLLECTION_FAILED" if command in (LSBLK, FINDMNT) else "LVM_COLLECTION_FAILED"
        )
        try:
            run = subprocess.run(
                command,
                capture_output=True,
                check=False,
                timeout=20,
                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"},
            )
        except (OSError, UnicodeError, subprocess.TimeoutExpired) as error:
            raise StorageProbeError(failure) from error
        if run.returncode != 0:
            raise StorageProbeError(failure)
        reports.append(run.stdout)
    try:
        pools = add_topology(
            summarize_backing(
                summarize(config, parse_lvs_report(reports[0])),
                parse_report(reports[1], "pv"),
                parse_report(reports[2], "vg"),
            ),
            reports[3],
            reports[4],
        )
    except TopologyError as error:
        raise StorageProbeError(str(error)) from error
    return {
        "schema_version": 1,
        "sample_finished_at": datetime.now(UTC).isoformat(),
        "thin_pools": pools,
        "admission_ready": False,
    }


def write_snapshot(report, path=SNAPSHOT):
    """Publish a root-owned snapshot in an operator-owned runtime directory."""
    if getattr(os, "geteuid", lambda: -1)() != 0:
        raise StorageProbeError("ROOT_REQUIRED")
    directory = path.parent
    info = directory.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise StorageProbeError("SNAPSHOT_DIRECTORY_UNSAFE")
    payload = (json.dumps(report, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    if len(payload) > MAX_INPUT:
        raise StorageProbeError("SNAPSHOT_TOO_LARGE")
    import grp

    group = grp.getgrnam("lab-node-agent").gr_gid
    name = None
    try:
        fd, name = tempfile.mkstemp(prefix=".storage-", dir=directory)
        with os.fdopen(fd, "wb") as output:
            output.write(payload)
            output.flush()
            os.fchown(output.fileno(), 0, group)
            os.fchmod(output.fileno(), 0o640)
            os.fsync(output.fileno())
        os.replace(name, path)
        name = None
        dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if name is not None:
            os.unlink(name)


def read_snapshot(path=SNAPSHOT, now=None):
    """Return only a fresh root-owned regular file; any uncertainty is an error."""
    now = now or datetime.now(UTC)
    if not all(hasattr(os, name) for name in ("O_NOFOLLOW", "O_NONBLOCK", "getegid")):
        raise StorageProbeError("SNAPSHOT_UNSUPPORTED_PLATFORM")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != 0
                or info.st_gid != os.getegid()
                or info.st_mode & 0o137
                or info.st_size > MAX_INPUT
            ):
                raise StorageProbeError("SNAPSHOT_UNTRUSTED")
            raw = source.read(MAX_INPUT + 1)
        report = json.loads(raw)
        if set(report) != {"schema_version", "sample_finished_at", "thin_pools", "admission_ready"}:
            raise ValueError("Unexpected snapshot schema")
        if report["schema_version"] != 1 or report["admission_ready"] is not False:
            raise ValueError("Unexpected snapshot version")
        sampled = datetime.fromisoformat(report["sample_finished_at"])
        age = now - sampled
        if sampled.tzinfo is None or not -timedelta(seconds=5) <= age <= MAX_SNAPSHOT_AGE:
            raise StorageProbeError("SNAPSHOT_STALE")
        pools = report["thin_pools"]
        if not isinstance(pools, list) or len(pools) > 128:
            raise ValueError("Invalid pools")
        seen_vg_uuids = set()
        seen_pv_uuids = set()
        for pool in pools:
            if not isinstance(pool, dict) or pool.get("ownership_reconciled") is not False:
                raise ValueError("Invalid pool")
            identifier(pool["storage"])
            identifier(pool["vgname"])
            identifier(pool["thinpool"])
            nonnegative_integer(pool["pool_size_bytes"])
            percent(pool["data_percent"])
            percent(pool["metadata_percent"])
            backing = pool.get("backing")
            if backing is not None:
                if (
                    not isinstance(backing, dict)
                    or backing.get("physical_backing_reconciled") is not False
                    or not isinstance(backing.get("physical_volumes"), list)
                    or len(backing["physical_volumes"]) > 128
                ):
                    raise ValueError("Invalid backing")
                nonnegative_integer(backing["vg_size_bytes"])
                nonnegative_integer(backing["vg_unallocated_bytes"])
                has_uuid = "vg_uuid" in backing
                if has_uuid:
                    vg_uuid = lvm_uuid(backing["vg_uuid"])
                    if vg_uuid in seen_vg_uuids:
                        raise ValueError("Duplicate VG UUID")
                    seen_vg_uuids.add(vg_uuid)
                if (
                    not backing["physical_volumes"]
                    or backing["vg_size_bytes"] < pool["pool_size_bytes"]
                ):
                    raise ValueError("Invalid backing size")
                seen_devices = set()
                for device in backing["physical_volumes"]:
                    device_path(device["name"])
                    if ("pv_uuid" in device) != has_uuid:
                        raise ValueError("Incomplete LVM UUIDs")
                    if has_uuid:
                        pv_uuid = lvm_uuid(device["pv_uuid"])
                        if pv_uuid in seen_pv_uuids:
                            raise ValueError("Duplicate PV UUID")
                        seen_pv_uuids.add(pv_uuid)
                    nonnegative_integer(device["size_bytes"])
                    nonnegative_integer(device["unallocated_bytes"])
                    if device["name"] in seen_devices:
                        raise ValueError("Duplicate PV")
                    seen_devices.add(device["name"])
                    topology = device.get("topology")
                    if topology is not None:
                        if (
                            not isinstance(topology, dict)
                            or set(topology)
                            != {
                                "type",
                                "size_bytes",
                                "backing_disks",
                                "whole_disk",
                                "shares_system_disk",
                            }
                            or not isinstance(topology["type"], str)
                            or not 1 <= len(topology["type"]) <= 32
                            or type(topology["whole_disk"]) is not bool
                            or type(topology["shares_system_disk"]) is not bool
                            or not isinstance(topology["backing_disks"], list)
                            or not 1 <= len(topology["backing_disks"]) <= 128
                            or len(set(topology["backing_disks"])) != len(topology["backing_disks"])
                        ):
                            raise ValueError("Invalid block topology")
                        nonnegative_integer(topology["size_bytes"])
                        if topology["size_bytes"] < device["size_bytes"]:
                            raise ValueError("Invalid block size")
                        for disk in topology["backing_disks"]:
                            device_path(disk)
                        if topology["whole_disk"] and (
                            topology["type"] != "disk"
                            or topology["backing_disks"] != [device["name"]]
                        ):
                            raise ValueError("Invalid whole-disk topology")
                if (
                    sum(device["size_bytes"] for device in backing["physical_volumes"])
                    < backing["vg_size_bytes"]
                    or sum(device["unallocated_bytes"] for device in backing["physical_volumes"])
                    != backing["vg_unallocated_bytes"]
                ):
                    raise ValueError("Invalid backing totals")
            if not isinstance(pool["volumes"], list) or len(pool["volumes"]) > 10000:
                raise ValueError("Invalid volumes")
            for volume in pool["volumes"]:
                lv_identifier(volume["name"])
                nonnegative_integer(volume["size_bytes"])
                if volume.get("ownership") != "UNVERIFIED":
                    raise ValueError("Invalid ownership")
        if len({pool["storage"] for pool in pools}) != len(pools):
            raise ValueError("Duplicate pool")
        return report
    except (OSError, UnicodeError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise StorageProbeError("SNAPSHOT_INVALID") from error


def main():
    try:
        report = collect_local()
        if sys.argv[1:] == ["--write-snapshot"]:
            write_snapshot(report)
        elif not sys.argv[1:]:
            print(json.dumps(report, ensure_ascii=False))
        else:
            raise StorageProbeError("INVALID_ARGUMENTS")
    except (StorageProbeError, OSError, KeyError) as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
