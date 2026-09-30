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
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}")
LV_IDENTIFIER = re.compile(r"\[?[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}\]?")
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


def parse_lvs_report(raw):
    if len(raw) > MAX_INPUT:
        raise StorageProbeError("LVM_REPORT_TOO_LARGE")
    try:
        data = json.loads(raw)
        reports = data["report"]
        if not isinstance(reports, list) or len(reports) != 1:
            raise ValueError("Invalid report count")
        rows = reports[0]["lv"]
        if not isinstance(rows, list) or len(rows) > 10000:
            raise ValueError("Invalid LV count")
        return rows
    except (KeyError, TypeError, ValueError) as error:
        raise StorageProbeError("LVM_REPORT_INVALID") from error


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
        run = subprocess.run(
            LVS,
            capture_output=True,
            check=False,
            timeout=20,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"},
        )
    except (OSError, UnicodeError, subprocess.TimeoutExpired) as error:
        raise StorageProbeError("LVM_COLLECTION_FAILED") from error
    if run.returncode != 0:
        raise StorageProbeError("LVM_COLLECTION_FAILED")
    return {
        "schema_version": 1,
        "sample_finished_at": datetime.now(UTC).isoformat(),
        "thin_pools": summarize(config, parse_lvs_report(run.stdout)),
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
        for pool in pools:
            if not isinstance(pool, dict) or pool.get("ownership_reconciled") is not False:
                raise ValueError("Invalid pool")
            identifier(pool["storage"])
            identifier(pool["vgname"])
            identifier(pool["thinpool"])
            nonnegative_integer(pool["pool_size_bytes"])
            percent(pool["data_percent"])
            percent(pool["metadata_percent"])
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
