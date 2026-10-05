"""Build an exact LXC creation command from persisted lesson identities."""

import ipaddress
import re
import uuid


class LessonCommandRejected(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def create_lxc_payload(
    *, operation_id, runtime, binding, allocation, template, storage_name, ssh_public_key
):
    try:
        subnet = ipaddress.IPv4Network(allocation.cidr, strict=True)
        address = ipaddress.IPv4Address(runtime.guest_ipv4)
    except (TypeError, ValueError) as error:
        raise LessonCommandRejected("GUEST_ADDRESS_INVALID") from error
    if (
        not isinstance(operation_id, uuid.UUID)
        or operation_id.int == 0
        or runtime.kind != "LXC"
        or runtime.state != "PLANNED"
        or template.runtime_kind != "LXC"
        or not isinstance(template.source_ref, str)
        or not re.fullmatch(
            r"[A-Za-z][A-Za-z0-9_.-]{0,63}:vztmpl/debian-[A-Za-z0-9_.+-]+\.tar\.(?:zst|gz|xz)",
            template.source_ref,
        )
        or binding.runtime_id != runtime.id
        or binding.node_id != runtime.node_id
        or binding.generation != runtime.generation
        or binding.ownership_marker
        != f"lab-manager:runtime={runtime.id};generation={runtime.generation}"
        or allocation.id != runtime.network_allocation_id
        or allocation.node_id != runtime.node_id
        or allocation.environment_id != runtime.environment_id
        or allocation.state != "APPLIED"
        or address not in subnet
        or address in (subnet.network_address, subnet.broadcast_address, subnet.network_address + 1)
        or not isinstance(storage_name, str)
        or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", storage_name)
        or not isinstance(ssh_public_key, str)
        or not re.fullmatch(
            r"ssh-ed25519 [A-Za-z0-9+/=]{40,120}(?: [A-Za-z0-9_.@-]{1,64})?",
            ssh_public_key,
        )
    ):
        raise LessonCommandRejected("LXC_COMMAND_NOT_READY")
    return {
        "operation_id": str(operation_id),
        "node_id": str(runtime.node_id),
        "kind": "LXC_CREATE",
        "runtime_id": str(runtime.id),
        "generation": runtime.generation,
        "vmid": binding.vmid,
        "spec": {
            "hostname": f"lab-{runtime.id.hex[:12]}",
            "template": template.source_ref,
            "storage": storage_name,
            "segment": {
                "allocation_id": str(allocation.id),
                "mode": allocation.mode,
                "cidr": allocation.cidr,
            },
            "address": str(address),
            "memory_mib": runtime.memory_mib,
            "cores": runtime.vcpu,
            "disk_gib": runtime.disk_gib,
            "ssh_public_key": ssh_public_key,
        },
    }
