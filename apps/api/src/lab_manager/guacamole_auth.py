"""Short-lived Guacamole JSON-auth grants for owned SSH runtimes.

The grant is a login credential, not a session revocation mechanism. Callers
must authorize the actor and the current EnvironmentRun before issuing it.
"""

import base64
import hashlib
import hmac
import ipaddress
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from lab_manager.ipam import PRIVATE_POOLS

USERNAME = re.compile(r"[a-z_][a-z0-9_-]{0,31}\Z")
HOST_KEY = re.compile(r"ssh-(?:ed25519|rsa) [A-Za-z0-9+/=]{40,2048}(?: .*)?\Z")


class GuacamoleGrantError(ValueError):
    pass


@dataclass(frozen=True)
class SSHConnection:
    runtime_id: uuid.UUID
    address: str
    subnet: str
    username: str
    private_key: str
    host_key: str
    name: str = "Терминал"

    def payload(self) -> dict:
        try:
            address = ipaddress.IPv4Address(self.address)
            subnet = ipaddress.IPv4Network(self.subnet, strict=True)
        except ValueError as error:
            raise GuacamoleGrantError("INVALID_GUEST_ADDRESS") from error
        if (
            not isinstance(self.runtime_id, uuid.UUID)
            or self.runtime_id.int == 0
            or address not in subnet
            or address in (subnet.network_address, subnet.broadcast_address)
            or not any(subnet.subnet_of(pool) for pool in PRIVATE_POOLS)
            or not USERNAME.fullmatch(self.username)
            or not self.private_key.startswith("-----BEGIN OPENSSH PRIVATE KEY-----\n")
            or not self.private_key.rstrip().endswith("-----END OPENSSH PRIVATE KEY-----")
            or not HOST_KEY.fullmatch(self.host_key)
            or not 1 <= len(self.name) <= 80
        ):
            raise GuacamoleGrantError("INVALID_SSH_CONNECTION")
        return {
            "id": str(self.runtime_id),
            "protocol": "ssh",
            "parameters": {
                "hostname": str(address),
                "port": "22",
                "username": self.username,
                "private-key": self.private_key,
                "host-key": self.host_key,
            },
        }


@dataclass(frozen=True)
class JoinConnection:
    runtime_id: uuid.UUID
    read_only: bool = False
    name: str = "Совместный просмотр"

    def payload(self) -> dict:
        if (
            not isinstance(self.runtime_id, uuid.UUID)
            or self.runtime_id.int == 0
            or type(self.read_only) is not bool
            or not 1 <= len(self.name) <= 80
        ):
            raise GuacamoleGrantError("INVALID_JOIN_CONNECTION")
        return {
            "join": str(self.runtime_id),
            "parameters": {"read-only": "true" if self.read_only else "false"},
        }


def seal_json(secret_hex: str, payload: dict) -> str:
    """Apache guacamole-auth-json: HMAC-SHA256, then AES-128-CBC/PKCS7."""
    if not isinstance(secret_hex, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", secret_hex):
        raise GuacamoleGrantError("INVALID_GUACAMOLE_SECRET")
    key = bytes.fromhex(secret_hex)
    plaintext = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    signed = hmac.new(key, plaintext, hashlib.sha256).digest() + plaintext
    padder = padding.PKCS7(128).padder()
    padded = padder.update(signed) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(bytes(16))).encryptor()
    encrypted = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(encrypted).decode("ascii")


def issue_ssh_grant(
    secret_hex: str,
    *,
    actor_id: uuid.UUID,
    connection: SSHConnection | JoinConnection,
    now: datetime,
    lifetime: timedelta = timedelta(seconds=30),
) -> str:
    if (
        not isinstance(actor_id, uuid.UUID)
        or actor_id.int == 0
        or not isinstance(connection, (SSHConnection, JoinConnection))
        or now.tzinfo is None
        or not timedelta(0) < lifetime <= timedelta(seconds=60)
    ):
        raise GuacamoleGrantError("INVALID_GRANT_REQUEST")
    entry = connection.payload()
    payload = {
        "username": f"u-{actor_id.hex}",
        "expires": int((now + lifetime).timestamp() * 1000),
        "connections": {connection.name: entry},
    }
    return seal_json(secret_hex, payload)
