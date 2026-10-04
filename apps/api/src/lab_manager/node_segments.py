"""Pinned mTLS client for idempotent creation of closed lab bridges."""

import hashlib
import http.client
import json
import re
import ssl
import uuid
from urllib.parse import urlsplit

from lab_manager.node_transport import NodeEndpoint

MAX_BYTES = 4096
ERROR = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")


class NodeSegmentError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def bridge_name(allocation_id: uuid.UUID) -> str:
    value = int.from_bytes(hashlib.sha256(allocation_id.bytes).digest()[:8], "big") % 36**6
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    suffix = ""
    for _ in range(6):
        value, digit = divmod(value, 36)
        suffix = digits[digit] + suffix
    return "lmbr" + suffix


def validate_result(value, allocation_id: uuid.UUID, mode: str | None, cidr: str | None) -> dict:
    if (
        not isinstance(value, dict)
        or set(value) != {"allocation_id", "mode", "cidr", "bridge", "state"}
        or value["allocation_id"] != str(allocation_id)
        or value["bridge"] != bridge_name(allocation_id)
        or value["state"] != "CREATED"
        or value["mode"] not in ("ISOLATED", "GROUP_LAN")
        or not isinstance(value["cidr"], str)
        or (mode is not None and value["mode"] != mode)
        or (cidr is not None and value["cidr"] != cidr)
    ):
        raise NodeSegmentError("SEGMENT_RESPONSE_MISMATCH")
    return value


class NodeSegmentClient:
    def __init__(self, endpoint: NodeEndpoint):
        self.endpoint = endpoint

    def create(self, allocation_id: uuid.UUID, mode: str, cidr: str) -> dict:
        if (
            not isinstance(allocation_id, uuid.UUID)
            or allocation_id.int == 0
            or mode not in ("ISOLATED", "GROUP_LAN")
            or not isinstance(cidr, str)
        ):
            raise NodeSegmentError("INVALID_SEGMENT_SPEC")
        body = json.dumps(
            {"allocation_id": str(allocation_id), "mode": mode, "cidr": cidr},
            separators=(",", ":"),
        ).encode()
        return validate_result(
            self._request("POST", "/v1/segments", body), allocation_id, mode, cidr
        )

    def status(self, allocation_id: uuid.UUID) -> dict | None:
        if not isinstance(allocation_id, uuid.UUID) or allocation_id.int == 0:
            raise NodeSegmentError("INVALID_SEGMENT_ID")
        value = self._request("GET", f"/v1/segments/{allocation_id}", None, missing=True)
        return validate_result(value, allocation_id, None, None) if value is not None else None

    def _request(self, method: str, path: str, body: bytes | None, *, missing=False) -> dict | None:
        connection = None
        try:
            context = ssl.create_default_context(cafile=str(self.endpoint.ca))
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(str(self.endpoint.certificate), str(self.endpoint.key))
            url = urlsplit(self.endpoint.origin)
            connection = http.client.HTTPSConnection(
                url.hostname, url.port, context=context, timeout=10
            )
            connection.connect()
            if hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest() != (
                self.endpoint.server_sha256
            ):
                raise NodeSegmentError("NODE_CERTIFICATE_MISMATCH")
            headers = {"Accept": "application/json"}
            if body is not None:
                headers["Content-Type"] = "application/json"
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ValueError("oversized response")
            value = json.loads(raw)
            if response.status == 404 and missing:
                if value != {"error": "SEGMENT_NOT_FOUND"}:
                    raise ValueError("unexpected not found")
                return None
            if response.status in (400, 409, 503):
                code = value.get("error") if isinstance(value, dict) else None
                if isinstance(code, str) and ERROR.fullmatch(code):
                    raise NodeSegmentError(code)
                raise ValueError("invalid error")
            if response.status != (202 if method == "POST" else 200):
                raise ValueError("unexpected status")
            if not isinstance(value, dict):
                raise ValueError("invalid result")
            return value
        except NodeSegmentError:
            raise
        except Exception as error:
            raise NodeSegmentError("NODE_SEGMENT_TRANSPORT_FAILED") from error
        finally:
            if connection is not None:
                connection.close()
