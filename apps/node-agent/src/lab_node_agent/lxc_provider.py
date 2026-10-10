"""Bounded Proxmox LXC operations for a future durable lesson command worker.

This adapter has no route from the browser or the inventory endpoint. Its
caller must first reserve capacity, a subnet/address and a VMID, then record
the returned Proxmox task before considering the guest ready. The guest link
remains down until the segment gateway and firewall policy are ready.
"""

import ipaddress
import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass

from lab_node_agent.proxmox import ProxmoxConfig
from lab_node_agent.segments import GUEST_POOL

STORAGE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}")
POOL = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}")
TEMPLATE = re.compile(
    r"[A-Za-z][A-Za-z0-9_.-]{0,63}:vztmpl/debian-[A-Za-z0-9_.+-]+\.tar\.(?:zst|gz|xz)"
)
BRIDGE = re.compile(r"lmbr[a-z0-9]{6}")
HOSTNAME = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
UPID = re.compile(
    r"UPID:[A-Za-z0-9.-]+:(?:[0-9A-Fa-f]+:){3}[A-Za-z0-9_-]+:"
    r"[A-Za-z0-9_-]*:[A-Za-z0-9_.@!-]+:"
)
MAX_RESPONSE_BYTES = 1024 * 1024


class LxcOperationError(Exception):
    """Fixed error codes; never expose Proxmox response bodies or credentials."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise LxcOperationError("PROXMOX_REDIRECT_REFUSED")


@dataclass(frozen=True)
class CreateLxc:
    runtime_id: uuid.UUID
    generation: int
    vmid: int
    hostname: str
    template: str
    storage: str
    pool: str
    bridge: str
    cidr: str
    address: str
    memory_mib: int
    cores: int
    disk_gib: int
    ssh_public_key: str

    def __post_init__(self):
        try:
            subnet = ipaddress.IPv4Network(self.cidr, strict=True)
            address = ipaddress.IPv4Address(self.address)
            gateway = subnet.network_address + 1
            valid_address = (
                subnet.prefixlen <= 30
                and address in subnet
                and address not in (subnet.network_address, subnet.broadcast_address, gateway)
                and subnet.subnet_of(GUEST_POOL)
            )
        except (TypeError, ValueError):
            valid_address = False
        if (
            not isinstance(self.runtime_id, uuid.UUID)
            or self.runtime_id.int == 0
            or type(self.generation) is not int
            or not 1 <= self.generation <= 1000000
            or type(self.vmid) is not int
            or not 100 <= self.vmid <= 999999999
            or not HOSTNAME.fullmatch(self.hostname)
            or not TEMPLATE.fullmatch(self.template)
            or not STORAGE.fullmatch(self.storage)
            or not POOL.fullmatch(self.pool)
            or not BRIDGE.fullmatch(self.bridge)
            or not valid_address
            or type(self.memory_mib) is not int
            or not 128 <= self.memory_mib <= 262144
            or type(self.cores) is not int
            or not 1 <= self.cores <= 256
            or type(self.disk_gib) is not int
            or not 1 <= self.disk_gib <= 4096
            or not re.fullmatch(
                r"ssh-ed25519 [A-Za-z0-9+/=]{40,120}(?: [A-Za-z0-9_.@-]{1,64})?",
                self.ssh_public_key,
            )
        ):
            raise LxcOperationError("INVALID_LXC_SPEC")

    @property
    def marker(self):
        return f"lab-manager:runtime={self.runtime_id};generation={self.generation}"

    def form(self):
        subnet = ipaddress.IPv4Network(self.cidr)
        return {
            "vmid": str(self.vmid),
            "hostname": self.hostname,
            "ostemplate": self.template,
            "ostype": "debian",
            "rootfs": f"{self.storage}:{self.disk_gib}",
            "pool": self.pool,
            "memory": str(self.memory_mib),
            "cores": str(self.cores),
            "swap": "0",
            "unprivileged": "1",
            "onboot": "0",
            "start": "0",
            "net0": (
                f"name=eth0,bridge={self.bridge},firewall=1,"
                f"ip={self.address}/{subnet.prefixlen},gw={subnet.network_address + 1},"
                "ip6=manual,link_down=1"
            ),
            "ssh-public-keys": self.ssh_public_key,
            "description": self.marker,
        }


class ProxmoxLxcProvider:
    def __init__(self, config: ProxmoxConfig):
        self.config = config
        try:
            context = ssl.create_default_context(cafile=str(config.ca_file))
        except (OSError, ssl.SSLError) as error:
            raise LxcOperationError("PROXMOX_CA_INVALID") from error
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            NoRedirect(),
            urllib.request.HTTPSHandler(context=context),
        )

    def _request(self, method: str, path: str, form: dict[str, str] | None = None):
        if method not in ("GET", "POST", "PUT") or not (
            path.startswith(f"/nodes/{self.config.node}/lxc")
            or path.startswith(f"/nodes/{self.config.node}/tasks/UPID%3A")
        ):
            raise LxcOperationError("OPERATION_NOT_ALLOWED")
        data = urllib.parse.urlencode(form).encode() if form is not None else None
        request = urllib.request.Request(
            self.config.origin.rstrip("/") + "/api2/json" + path,
            method=method,
            data=data,
            headers={
                "Accept": "application/json",
                "Authorization": f"PVEAPIToken={self.config.token_id}={self.config.token_secret}",
                **({"Content-Type": "application/x-www-form-urlencoded"} if data else {}),
            },
        )
        try:
            with self.opener.open(request, timeout=15) as response:
                if response.status != 200:
                    raise LxcOperationError("PROXMOX_HTTP_ERROR")
                body = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            if error.code == 404 and method == "GET":
                return None
            code = "PROXMOX_ACCESS_DENIED" if error.code in (401, 403) else "PROXMOX_HTTP_ERROR"
            raise LxcOperationError(code) from None
        except (urllib.error.URLError, OSError, TimeoutError) as error:
            raise LxcOperationError("PROXMOX_CONNECTION_FAILED") from error
        if len(body) > MAX_RESPONSE_BYTES:
            raise LxcOperationError("PROXMOX_RESPONSE_TOO_LARGE")
        try:
            payload = json.loads(body)
        except (UnicodeError, ValueError, RecursionError) as error:
            raise LxcOperationError("PROXMOX_RESPONSE_INVALID") from error
        if not isinstance(payload, dict) or "data" not in payload:
            raise LxcOperationError("PROXMOX_RESPONSE_INVALID")
        return payload["data"]

    @staticmethod
    def _vmid(vmid: int):
        if type(vmid) is not int or not 100 <= vmid <= 999999999:
            raise LxcOperationError("INVALID_VMID")
        return vmid

    @staticmethod
    def _task(value):
        if not isinstance(value, str) or not UPID.fullmatch(value):
            raise LxcOperationError("PROXMOX_TASK_INVALID")
        return value

    def config_for(self, vmid: int):
        config = self._request("GET", f"/nodes/{self.config.node}/lxc/{self._vmid(vmid)}/config")
        if config is not None and not isinstance(config, dict):
            raise LxcOperationError("PROXMOX_RESPONSE_INVALID")
        return config

    def create(self, spec: CreateLxc):
        if not isinstance(spec, CreateLxc):
            raise LxcOperationError("INVALID_LXC_SPEC")
        existing = self.config_for(spec.vmid)
        if existing is not None:
            if str(existing.get("description", "")).rstrip("\r\n") != spec.marker:
                raise LxcOperationError("VMID_ALREADY_OWNED")
            net = str(existing.get("net0", "")).split(",")
            rootfs = str(existing.get("rootfs", ""))
            subnet = ipaddress.IPv4Network(spec.cidr)
            if (
                str(existing.get("unprivileged")) != "1"
                or str(existing.get("onboot", "0")) != "0"
                or str(existing.get("ostype")) != "debian"
                or str(existing.get("hostname")) != spec.hostname
                or str(existing.get("memory")) != str(spec.memory_mib)
                or str(existing.get("cores")) != str(spec.cores)
                or str(existing.get("swap", "0")) != "0"
                or not re.fullmatch(
                    rf"{re.escape(spec.storage)}:vm-{spec.vmid}-disk-[0-9]+,size={spec.disk_gib}G",
                    rootfs,
                )
                or not all(
                    value in net
                    for value in (
                        "name=eth0",
                        f"bridge={spec.bridge}",
                        "firewall=1",
                        f"ip={spec.address}/{subnet.prefixlen}",
                        f"gw={subnet.network_address + 1}",
                        "ip6=manual",
                        "link_down=1",
                    )
                )
            ):
                raise LxcOperationError("GUEST_CONFIGURATION_DRIFT")
            return None  # Existing guest requires task/inventory reconciliation.
        result = self._request("POST", f"/nodes/{self.config.node}/lxc", spec.form())
        return self._task(result)

    def _owned(self, vmid: int, runtime_id: uuid.UUID, generation: int):
        if (
            not isinstance(runtime_id, uuid.UUID)
            or runtime_id.int == 0
            or type(generation) is not int
            or not 1 <= generation <= 1000000
        ):
            raise LxcOperationError("INVALID_RUNTIME_ID")
        config = self.config_for(vmid)
        marker = f"lab-manager:runtime={runtime_id};generation={generation}"
        if config is None or str(config.get("description", "")).rstrip("\r\n") != marker:
            raise LxcOperationError("GUEST_OWNERSHIP_UNCONFIRMED")
        if (
            str(config.get("unprivileged")) != "1"
            or str(config.get("onboot", "0")) != "0"
            or not re.search(r"(?:^|,)bridge=lmbr[a-z0-9]{6}(?:,|$)", str(config.get("net0", "")))
            or not re.search(r"(?:^|,)firewall=1(?:,|$)", str(config.get("net0", "")))
        ):
            raise LxcOperationError("GUEST_CONFIGURATION_DRIFT")
        return config

    def start(self, vmid: int, runtime_id: uuid.UUID, generation: int):
        config = self._owned(vmid, runtime_id, generation)
        net = str(config.get("net0", ""))
        fields = net.split(",")
        if (
            fields.count("link_down=1") != 1
            or not any(field.startswith("hwaddr=") for field in fields)
            or "type=veth" not in fields
        ):
            raise LxcOperationError("GUEST_NETWORK_DRIFT")
        enabled = ",".join(field for field in fields if field != "link_down=1")
        self._request("PUT", f"/nodes/{self.config.node}/lxc/{vmid}/config", {"net0": enabled})
        updated = self._owned(vmid, runtime_id, generation)
        if str(updated.get("net0", "")) != enabled:
            raise LxcOperationError("GUEST_NETWORK_DRIFT")
        return self._task(
            self._request("POST", f"/nodes/{self.config.node}/lxc/{vmid}/status/start")
        )

    def shutdown(self, vmid: int, runtime_id: uuid.UUID, generation: int):
        self._owned(vmid, runtime_id, generation)
        return self._task(
            self._request(
                "POST", f"/nodes/{self.config.node}/lxc/{vmid}/status/shutdown", {"timeout": "60"}
            )
        )

    def task_status(self, upid: str):
        self._task(upid)
        encoded = urllib.parse.quote(upid, safe="")
        result = self._request("GET", f"/nodes/{self.config.node}/tasks/{encoded}/status")
        if not isinstance(result, dict) or result.get("status") not in ("running", "stopped"):
            raise LxcOperationError("PROXMOX_TASK_STATUS_INVALID")
        return result

    def current_status(self, vmid: int):
        result = self._request(
            "GET", f"/nodes/{self.config.node}/lxc/{self._vmid(vmid)}/status/current"
        )
        if not isinstance(result, dict) or result.get("status") not in ("running", "stopped"):
            raise LxcOperationError("PROXMOX_GUEST_STATUS_INVALID")
        return result["status"]
