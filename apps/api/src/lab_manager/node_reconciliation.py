"""Derive conservative admission facts from a signed node sample and durable bindings."""

import re
from datetime import datetime

MARKER = re.compile(
    r"lab-manager:runtime=([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12});generation=([1-9][0-9]{0,6})\Z"
)


def reconcile(sample, bindings, disks):
    """Enrich a copy; uncertainty closes admission instead of guessing ownership.

    Root-held live firewall evidence is necessary but not sufficient: this
    process also requires matching bridges, guest ownership and disk claims.
    """
    sample = {**sample, "storages": [dict(item) for item in sample["storages"]]}
    limitations = set(sample.get("limitations", []))
    guests = sample.get("guests", [])
    by_vmid = {item.vmid: item for item in bindings}
    observed = set()
    ownership_ok = len(by_vmid) == len(bindings)
    external_memory = external_cpu = 0
    try:
        for guest in guests:
            vmid = guest["vmid"]
            if type(vmid) is not int or vmid in observed:
                raise ValueError("guest identity")
            observed.add(vmid)
            marker = guest.get("ownership_marker")
            binding = by_vmid.get(vmid)
            if binding is None:
                if marker is not None:
                    ownership_ok = False
            elif (
                marker != binding.ownership_marker
                or not isinstance(marker, str)
                or not MARKER.fullmatch(marker)
                or marker
                != f"lab-manager:runtime={binding.runtime_id};generation={binding.generation}"
            ):
                ownership_ok = False
            if guest.get("reported_status") == "running":
                memory = guest.get("memory_limit_bytes")
                cpus = guest.get("reported_vcpus")
                if type(memory) is not int or memory <= 0 or type(cpus) is not int or cpus <= 0:
                    raise ValueError("running guest resources")
                # Count every running guest in addition to bookings until a
                # run/guest association can be proven from the active ledger.
                external_memory += (memory + 2**20 - 1) // 2**20
                external_cpu += cpus * 1000
            elif guest.get("reported_status") != "stopped":
                raise ValueError("guest status")
        if set(by_vmid) != observed.intersection(by_vmid):
            ownership_ok = False
    except (KeyError, TypeError, ValueError):
        ownership_ok = False
        external_memory = external_cpu = 0

    sample["ownership_reconciled"] = ownership_ok
    sample["external_running_memory_mib"] = external_memory
    sample["external_cpu_millicredits"] = external_cpu
    if ownership_ok:
        limitations.discard("GUEST_OWNERSHIP_NOT_RECONCILED")
    else:
        limitations.add("GUEST_OWNERSHIP_NOT_RECONCILED")

    pools = {pool["storage"]: pool for pool in sample.get("local_thin_pools", [])}
    for storage in sample["storages"]:
        storage["commitments_reconciled"] = False
        storage["external_committed_bytes"] = 0
        pool = pools.get(storage["name"])
        if storage.get("backend") != "lvmthin" or pool is None:
            continue
        try:
            sampled = datetime.fromisoformat(sample["sample_finished_at"])
            stored = datetime.fromisoformat(sample["local_thin_sample_finished_at"])
            if (
                sampled.tzinfo is None
                or stored.tzinfo is None
                or not 0 <= (sampled - stored).total_seconds() <= 120
                or "LOCAL_STORAGE_MISMATCH" in limitations
            ):
                continue
            volumes = pool["volumes"]
            by_ref = {f"{storage['name']}:{volume['name']}": volume for volume in volumes}
            if len(by_ref) != len(volumes):
                continue
            expected = [
                disk
                for disk in disks
                if disk.storage_name == storage["name"] and disk.state != "DELETED"
            ]
            present = [disk for disk in expected if disk.state == "PRESENT"]
            if any(disk.state not in ("PLANNED", "PRESENT") for disk in expected):
                continue
            refs = [disk.provider_ref for disk in present]
            if (
                None in refs
                or len(set(refs)) != len(refs)
                or any(ref not in by_ref for ref in refs)
            ):
                continue
            if any(
                type(by_ref[disk.provider_ref]["size_bytes"]) is not int
                or by_ref[disk.provider_ref]["size_bytes"] != disk.logical_bytes
                for disk in present
            ):
                continue
            external = [volume for ref, volume in by_ref.items() if ref not in refs]
            if any(
                type(item["size_bytes"]) is not int or item["size_bytes"] < 0 for item in external
            ):
                continue
            storage["external_committed_bytes"] = sum(item["size_bytes"] for item in external)
            storage["commitments_reconciled"] = True
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    if all(
        item["commitments_reconciled"]
        for item in sample["storages"]
        if item.get("backend") == "lvmthin"
    ):
        limitations.discard("DISK_COMMITMENTS_NOT_RECONCILED")
    security = sample.get("network_security")
    guacamole = security.get("guacamole") if isinstance(security, dict) else None
    bridges = sample.get("network_bridges")
    isolated_bridge = (
        isinstance(guacamole, dict)
        and isinstance(bridges, list)
        and len(
            [
                item
                for item in bridges
                if isinstance(item, dict)
                and item.get("name") == guacamole.get("bridge")
                and item.get("active") is True
                and item.get("ports") == []
            ]
        )
        == 1
    )
    ready = (
        sample.get("admission_ready") is True
        and isinstance(security, dict)
        and security.get("ready") is True
        and isolated_bridge
        and ownership_ok
        and any(item.get("backend") == "lvmthin" for item in sample["storages"])
        and all(
            item.get("commitments_reconciled") is True
            for item in sample["storages"]
            if item.get("backend") == "lvmthin"
        )
        and "LOCAL_STORAGE_MISMATCH" not in limitations
        and "NETWORK_INVENTORY_UNAVAILABLE" not in limitations
    )
    if ready:
        limitations.discard("NETWORK_AND_GATEWAY_NOT_VERIFIED")
    else:
        limitations.add("NETWORK_AND_GATEWAY_NOT_VERIFIED")
    sample["limitations"] = sorted(limitations)
    sample["admission_ready"] = ready
    return sample
