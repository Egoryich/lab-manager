"""Read-only, local LVM-thin diagnostic for a future reconciled node inventory.

This probe never marks a pool admissible. It is run locally by an administrator;
the mTLS agent does not execute privileged commands or accept probe parameters.
"""

import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
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


def main():
    try:
        print(json.dumps(collect_local(), ensure_ascii=False))
    except StorageProbeError as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
