"""Read-only, bounded Proxmox REST adapter with mandatory certificate verification."""

import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

MAX_RESPONSE_BYTES = 4 * 1024 * 1024
NODE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,62}")


class InventoryError(Exception):
    """Only fixed error codes cross the CLI boundary; no raw server response or secret."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise InventoryError("PROXMOX_REDIRECT_REFUSED")


@dataclass(frozen=True)
class ProxmoxConfig:
    origin: str
    node: str
    ca_file: Path
    token_id: str = field(repr=False)
    token_secret: str = field(repr=False)

    def __post_init__(self):
        try:
            url = urllib.parse.urlsplit(self.origin)
            valid = (
                url.scheme == "https"
                and url.hostname
                and url.port
                and 1 <= url.port <= 65535
                and url.path in ("", "/")
                and not url.query
                and not url.fragment
                and not url.username
                and not url.password
                and not any(c.isspace() or ord(c) < 32 for c in self.origin)
            )
        except ValueError:
            valid = False
        if not valid or not NODE_NAME.fullmatch(self.node):
            raise InventoryError("INVALID_PROXMOX_CONFIGURATION")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+![A-Za-z0-9_.-]+", self.token_id):
            raise InventoryError("INVALID_TOKEN_ID")
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", self.token_secret):
            raise InventoryError("INVALID_TOKEN_SECRET")
        if not self.ca_file.is_absolute() or not self.ca_file.is_file():
            raise InventoryError("PROXMOX_CA_REQUIRED")


class ProxmoxReader:
    def __init__(self, config: ProxmoxConfig):
        self.config = config
        try:
            context = ssl.create_default_context(cafile=str(config.ca_file))
        except (OSError, ssl.SSLError) as error:
            raise InventoryError("PROXMOX_CA_INVALID") from error
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        # Ignore proxy environment variables; never send an API token through a proxy.
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            NoRedirect(),
            urllib.request.HTTPSHandler(context=context),
        )

    def get(self, resource: str):
        # Callers cannot supply arbitrary paths, URLs, parameters, or write methods.
        paths = {
            "version": "/version",
            "permissions": "/access/permissions?path=%2F",
            "status": f"/nodes/{self.config.node}/status",
            "storage": f"/nodes/{self.config.node}/storage",
            "network": f"/nodes/{self.config.node}/network",
            "qemu": f"/nodes/{self.config.node}/qemu",
            "lxc": f"/nodes/{self.config.node}/lxc",
        }
        if resource not in paths:
            raise InventoryError("RESOURCE_NOT_ALLOWED")
        return self._get_path(paths[resource])

    def guest_config(self, kind: str, vmid: int):
        """Read one exact guest config for future ownership reconciliation."""
        if kind not in ("QEMU", "LXC") or type(vmid) is not int or not 100 <= vmid <= 999999999:
            raise InventoryError("RESOURCE_NOT_ALLOWED")
        resource = "qemu" if kind == "QEMU" else "lxc"
        data = self._get_path(f"/nodes/{self.config.node}/{resource}/{vmid}/config")
        if not isinstance(data, dict):
            raise InventoryError("PROXMOX_RESPONSE_INVALID")
        return data

    def _get_path(self, path: str):
        request = urllib.request.Request(
            self.config.origin.rstrip("/") + "/api2/json" + path,
            method="GET",
            headers={
                "Accept": "application/json",
                "Authorization": f"PVEAPIToken={self.config.token_id}={self.config.token_secret}",
            },
        )
        try:
            with self.opener.open(request, timeout=5) as response:
                if response.status != 200:
                    raise InventoryError("PROXMOX_HTTP_ERROR")
                body = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            code = "PROXMOX_ACCESS_DENIED" if error.code in (401, 403) else "PROXMOX_HTTP_ERROR"
            raise InventoryError(code) from None
        except (urllib.error.URLError, OSError, TimeoutError) as error:
            raise InventoryError("PROXMOX_CONNECTION_FAILED") from error
        if len(body) > MAX_RESPONSE_BYTES:
            raise InventoryError("PROXMOX_RESPONSE_TOO_LARGE")
        try:
            result = json.loads(body)
        except (ValueError, UnicodeError, RecursionError) as error:
            raise InventoryError("PROXMOX_RESPONSE_INVALID") from error
        if not isinstance(result, dict) or "data" not in result:
            raise InventoryError("PROXMOX_RESPONSE_INVALID")
        return result["data"]
