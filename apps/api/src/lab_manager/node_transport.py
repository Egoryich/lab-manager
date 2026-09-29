"""Pinned mTLS pull; endpoint configuration is operator-owned, never browser input."""

import hashlib
import http.client
import ipaddress
import json
import re
import ssl
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

MAX_BYTES = 4 * 1024 * 1024


class NodeTransportError(Exception):
    pass


@dataclass(frozen=True)
class NodeEndpoint:
    id: uuid.UUID
    name: str
    origin: str
    node: str
    server_sha256: str
    ca: Path
    certificate: Path
    key: Path


def load_endpoints(path):
    if path is None:
        return []
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or len(data) > 16:
        raise ValueError("Invalid node configuration")
    endpoints = []
    for item in data:
        url = urlsplit(item["origin"])
        try:
            address = ipaddress.ip_address(url.hostname or "")
        except ValueError as error:
            raise ValueError("Node origin must use a private IP address") from error
        if (
            url.scheme != "https"
            or not url.hostname
            or not url.port
            or address.is_unspecified
            or address.is_multicast
            or address.is_global
            or url.username
            or url.password
            or url.path
            or url.query
            or url.fragment
        ):
            raise ValueError("Invalid node origin")
        if not re.fullmatch(r"[a-f0-9]{64}", item["server_sha256"]):
            raise ValueError("Invalid server fingerprint")
        if not 1 <= len(item["name"]) <= 100 or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9.-]{0,62}", item["node"]
        ):
            raise ValueError("Invalid node name")
        files = [Path(item[k]) for k in ("ca", "certificate", "key")]
        if not all(p.is_absolute() and p.is_file() for p in files):
            raise ValueError("Missing TLS files")
        endpoints.append(
            NodeEndpoint(
                uuid.UUID(item["id"]),
                item["name"],
                item["origin"],
                item["node"],
                item["server_sha256"],
                *files,
            )
        )
    if len({e.id for e in endpoints}) != len(endpoints):
        raise ValueError("Duplicate node identity")
    return endpoints


class Observation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_id: uuid.UUID
    agent_boot_id: uuid.UUID
    sample: dict


class HostObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    memory_total_bytes: int = Field(ge=0)
    memory_used_bytes: int = Field(ge=0)
    memory_free_bytes: int = Field(ge=0)
    logical_cpus: int = Field(ge=1)
    cores_reported: int | None = Field(ge=1)
    sockets: int | None = Field(ge=1)
    uptime_seconds: int = Field(ge=0)


def validate_observation(data, endpoint, now=None):
    result = Observation.model_validate(data)
    sample = result.sample
    if result.node_id != endpoint.id or sample.get("node") != endpoint.node:
        raise NodeTransportError("NODE_IDENTITY_MISMATCH")
    if sample.get("protocol_version") != 1 or sample.get("admission_ready") is not False:
        raise NodeTransportError("UNSUPPORTED_INVENTORY")
    uuid.UUID(sample["snapshot_id"])
    start = datetime.fromisoformat(sample["sample_started_at"])
    end = datetime.fromisoformat(sample["sample_finished_at"])
    now = now or datetime.now(UTC)
    if start.tzinfo is None or end.tzinfo is None or not start <= end:
        raise NodeTransportError("INVALID_SAMPLE_TIME")
    if not -10 <= (now - start).total_seconds() <= 120 or end > now + timedelta(seconds=10):
        raise NodeTransportError("STALE_SAMPLE")
    HostObservation.model_validate(sample["host"])
    for key in ("storages", "guests", "limitations"):
        if not isinstance(sample.get(key), list) or len(sample[key]) > 10000:
            raise NodeTransportError("INVALID_SAMPLE")
    return result.model_dump(mode="json"), end


def fetch(endpoint):
    connection = None
    try:
        context = ssl.create_default_context(cafile=str(endpoint.ca))
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(str(endpoint.certificate), str(endpoint.key))
        url = urlsplit(endpoint.origin)
        connection = http.client.HTTPSConnection(url.hostname, url.port, context=context, timeout=3)
        connection.connect()
        peer = connection.sock.getpeercert(binary_form=True)
        if hashlib.sha256(peer).hexdigest() != endpoint.server_sha256:
            raise NodeTransportError("NODE_CERTIFICATE_MISMATCH")
        connection.request("GET", "/v1/inventory", headers={"Accept": "application/json"})
        response = connection.getresponse()
        if response.status != 200:
            raise NodeTransportError("NODE_INVENTORY_UNAVAILABLE")
        deadline = time.monotonic() + 12
        chunks = []
        size = 0
        while size <= MAX_BYTES:
            if time.monotonic() >= deadline:
                raise NodeTransportError("NODE_RESPONSE_TIMEOUT")
            chunk = response.read1(min(65536, MAX_BYTES + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            chunks.append(chunk)
        body = b"".join(chunks)
        if len(body) > MAX_BYTES:
            raise NodeTransportError("NODE_RESPONSE_TOO_LARGE")
        return validate_observation(json.loads(body), endpoint)
    except NodeTransportError:
        raise
    except Exception:
        raise NodeTransportError("NODE_CONNECTION_FAILED") from None
    finally:
        if connection:
            connection.close()
