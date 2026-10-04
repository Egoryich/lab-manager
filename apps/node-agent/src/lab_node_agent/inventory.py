"""Explicit observations, never physical admission guarantees or ownership guesses."""

import re
import uuid
from datetime import UTC, datetime

from lab_node_agent.proxmox import InventoryError

OWNER_MARKER = re.compile(
    r"lab-manager:runtime=([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12});generation=([1-9][0-9]{0,6})\Z"
)


def ownership_marker(config):
    description = object_value(config).get("description", "")
    if not isinstance(description, str) or len(description) > 1024:
        raise InventoryError("INVALID_GUEST_DESCRIPTION")
    marker = description.rstrip("\r\n")
    if not marker.startswith("lab-manager:"):
        return None
    match = OWNER_MARKER.fullmatch(marker)
    if not match or uuid.UUID(match.group(1)).int == 0 or int(match.group(2)) > 1000000:
        raise InventoryError("INVALID_LAB_OWNERSHIP_MARKER")
    return marker


def object_value(value):
    if not isinstance(value, dict):
        raise InventoryError("PROXMOX_RESPONSE_INVALID")
    return value


def array_value(value):
    if not isinstance(value, list) or len(value) > 10000:
        raise InventoryError("PROXMOX_RESPONSE_INVALID")
    return value


def count(value, *, optional=False):
    if value is None and optional:
        return None
    if type(value) is not int or value < 0:
        raise InventoryError("PROXMOX_RESPONSE_INVALID")
    return value


def label(value):
    if not isinstance(value, str) or not value or len(value) > 256:
        raise InventoryError("PROXMOX_RESPONSE_INVALID")
    return value


def bridges(value):
    result = []
    for raw in array_value(value):
        item = object_value(raw)
        if item.get("type") != "bridge":
            continue
        ports = item.get("bridge_ports")
        if not isinstance(ports, str) or len(ports) > 1024:
            raise InventoryError("PROXMOX_RESPONSE_INVALID")
        result.append(
            {
                "name": label(item.get("iface")),
                "active": item.get("active") == 1,
                "ports": [] if ports in ("", "none") else ports.split(),
            }
        )
    return result


def collect(reader, local_storage=None):
    started = datetime.now(UTC)
    # Lists are permission-filtered. Refuse a token missing global audit grants.
    permissions = object_value(reader.get("permissions"))
    root = object_value(permissions.get("/", {}))
    if not all(root.get(p) in (1, True) for p in ("Sys.Audit", "VM.Audit", "Datastore.Audit")):
        raise InventoryError("GLOBAL_AUDIT_PERMISSIONS_REQUIRED")
    version = object_value(reader.get("version"))
    status = object_value(reader.get("status"))
    memory = object_value(status.get("memory"))
    cpu = object_value(status.get("cpuinfo"))
    storages = []
    for raw in array_value(reader.get("storage")):
        item = object_value(raw)
        storages.append(
            {
                "name": label(item.get("storage")),
                "backend": label(item.get("type")),
                "active": item.get("active") == 1,
                "total_bytes": count(item.get("total"), optional=True),
                "used_bytes": count(item.get("used"), optional=True),
                "available_bytes": count(item.get("avail"), optional=True),
                "thin_metadata_percent": None,
            }
        )
    guests = []
    ids = set()
    for kind, resource in (("QEMU", "qemu"), ("LXC", "lxc")):
        for raw in array_value(reader.get(resource)):
            item = object_value(raw)
            vmid = count(item.get("vmid"))
            if vmid in ids:
                # E.g. a guest moved during non-atomic collection. Do not hide uncertainty.
                raise InventoryError("INCONSISTENT_GUEST_LIST")
            ids.add(vmid)
            # A marker is only a claim. VPS must match it to a durable runtime
            # binding before it may consider ownership reconciled.
            marker = ownership_marker(reader.guest_config(kind, vmid))
            reported = item.get("status")
            guests.append(
                {
                    "vmid": vmid,
                    "kind": kind,
                    "name": item.get("name") or None,
                    "reported_status": reported
                    if reported in ("running", "stopped")
                    else "unknown",
                    "memory_limit_bytes": count(item.get("maxmem"), optional=True),
                    "reported_vcpus": count(item.get("cpus"), optional=True),
                    # maxdisk is not a complete sum of all disks; never treat it as a ledger.
                    "reported_maxdisk_bytes": count(item.get("maxdisk"), optional=True),
                    "ownership": "UNVERIFIED",
                    "ownership_marker": marker,
                }
            )
    limitations = [
        "NON_ATOMIC_OBSERVATION",
        "ACL_FILTERING_POSSIBLE",
        "DISK_COMMITMENTS_NOT_RECONCILED",
        "NETWORK_AND_GATEWAY_NOT_VERIFIED",
    ]
    try:
        network_bridges = bridges(reader.get("network"))
    except InventoryError:
        network_bridges = []
        limitations.append("NETWORK_INVENTORY_UNAVAILABLE")
    local_pools = []
    if local_storage is None:
        limitations.append("LOCAL_STORAGE_SNAPSHOT_UNAVAILABLE")
    else:
        by_name = {pool["storage"]: pool for pool in local_storage["thin_pools"]}
        observed = {storage["name"]: storage for storage in storages}
        if any(
            name not in observed
            or observed[name]["backend"] != "lvmthin"
            or observed[name]["total_bytes"] != pool["pool_size_bytes"]
            or not observed[name]["active"]
            for name, pool in by_name.items()
        ) or any(
            storage["backend"] == "lvmthin" and storage["name"] not in by_name
            for storage in storages
        ):
            limitations.append("LOCAL_STORAGE_MISMATCH")
        else:
            for name, pool in by_name.items():
                observed[name]["thin_metadata_percent"] = pool["metadata_percent"]
            local_pools = local_storage["thin_pools"]
    return {
        "protocol_version": 1,
        "snapshot_id": str(uuid.uuid4()),
        "node": reader.config.node,
        "sample_started_at": started.isoformat(),
        "sample_finished_at": datetime.now(UTC).isoformat(),
        "proxmox_version": label(version.get("version")),
        "host": {
            "memory_total_bytes": count(memory.get("total")),
            "memory_used_bytes": count(memory.get("used")),
            "memory_free_bytes": count(memory.get("free")),
            "logical_cpus": count(cpu.get("cpus")),
            "cores_reported": count(cpu.get("cores"), optional=True),
            "sockets": count(cpu.get("sockets"), optional=True),
            "uptime_seconds": count(status.get("uptime")),
        },
        "storages": storages,
        "guests": guests,
        "network_bridges": network_bridges,
        "local_thin_pools": local_pools,
        "admission_ready": False,
        "limitations": limitations,
    }
