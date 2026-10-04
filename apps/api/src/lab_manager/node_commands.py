"""Pinned mTLS client for non-replayable Proxmox node commands.

The caller persists an operation and its exact payload before submit. A lost
POST response is uncertain; this module never retries it automatically.
"""

import hashlib
import http.client
import json
import re
import ssl
import uuid
from urllib.parse import urlsplit

from lab_manager.node_transport import NodeEndpoint

MAX_RESPONSE = 16 * 1024
KINDS = frozenset({"LXC_CREATE", "LXC_START", "LXC_SHUTDOWN"})
STATES = frozenset({"INTENT", "SUBMITTED", "SUCCEEDED", "FAILED", "UNCERTAIN"})


class NodeCommandError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class NodeCommandRejected(NodeCommandError):
    """The node returned a definitive application-level refusal."""


class NodeCommandUncertain(NodeCommandError):
    """Submission may have reached the node; read its receipt before action."""


class NodeCommandUnavailable(NodeCommandError):
    """A read-only receipt lookup failed and can be retried later."""


def _receipt(value, operation_id: uuid.UUID) -> dict:
    if not isinstance(value, dict):
        raise ValueError("receipt object")
    required = {
        "operation_id",
        "kind",
        "vmid",
        "runtime_id",
        "generation",
        "state",
        "task_id",
        "error_code",
    }
    if not required <= value.keys() or set(value) - required - {"task_status"}:
        raise ValueError("receipt fields")
    if (
        uuid.UUID(value["operation_id"]) != operation_id
        or value["kind"] not in KINDS
        or type(value["vmid"]) is not int
        or not 100 <= value["vmid"] <= 999999999
        or uuid.UUID(value["runtime_id"]).int == 0
        or type(value["generation"]) is not int
        or not 1 <= value["generation"] <= 1000000
        or value["state"] not in STATES
        or (value["task_id"] is not None and not isinstance(value["task_id"], str))
        or (value["error_code"] is not None and not isinstance(value["error_code"], str))
        or (
            "task_status" in value
            and value["task_status"] is not None
            and not isinstance(value["task_status"], str)
        )
    ):
        raise ValueError("receipt values")
    return value


class NodeCommandClient:
    def __init__(self, endpoint: NodeEndpoint):
        self.endpoint = endpoint

    def submit(self, command: dict) -> dict:
        try:
            if not isinstance(command, dict):
                raise ValueError("command object")
            operation_id = uuid.UUID(command["operation_id"])
            if uuid.UUID(command["node_id"]) != self.endpoint.id or command["kind"] not in KINDS:
                raise ValueError("command target")
            body = json.dumps(command, sort_keys=True, separators=(",", ":")).encode()
            if len(body) > MAX_RESPONSE:
                raise ValueError("command too large")
        except (TypeError, ValueError, KeyError):
            raise NodeCommandRejected("INVALID_COMMAND") from None
        receipt = self._request("POST", "/v1/commands", operation_id, body)
        if (
            receipt["kind"] != command["kind"]
            or receipt["vmid"] != command.get("vmid")
            or receipt["runtime_id"] != command.get("runtime_id")
            or receipt["generation"] != command.get("generation")
        ):
            raise NodeCommandUncertain("NODE_COMMAND_RECEIPT_MISMATCH")
        return receipt

    def status(self, operation_id: uuid.UUID) -> dict:
        if not isinstance(operation_id, uuid.UUID) or operation_id.int == 0:
            raise NodeCommandRejected("INVALID_OPERATION_ID")
        return self._request("GET", f"/v1/commands/{operation_id}", operation_id, None)

    def _request(self, method: str, path: str, operation_id: uuid.UUID, body: bytes | None):
        connection = None
        try:
            context = ssl.create_default_context(cafile=str(self.endpoint.ca))
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(str(self.endpoint.certificate), str(self.endpoint.key))
            url = urlsplit(self.endpoint.origin)
            connection = http.client.HTTPSConnection(
                url.hostname, url.port, context=context, timeout=20
            )
            connection.connect()
            cert = connection.sock.getpeercert(binary_form=True)
            if hashlib.sha256(cert).hexdigest() != self.endpoint.server_sha256:
                raise NodeCommandError("NODE_CERTIFICATE_MISMATCH")
            headers = {"Accept": "application/json"}
            if body is not None:
                headers["Content-Type"] = "application/json"
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise ValueError("oversized response")
            payload = json.loads(raw)
            if response.status == 503 and method == "GET":
                raise NodeCommandUnavailable("NODE_COMMAND_UNAVAILABLE")
            if response.status in (400, 404, 409):
                code = payload.get("error") if isinstance(payload, dict) else None
                if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code):
                    raise NodeCommandRejected(code)
                raise ValueError("invalid rejection")
            if response.status != (202 if method == "POST" else 200):
                raise ValueError("unexpected status")
            return _receipt(payload, operation_id)
        except NodeCommandRejected:
            raise
        except NodeCommandError as error:
            failure = NodeCommandUncertain if method == "POST" else NodeCommandUnavailable
            raise failure(error.code) from None
        except Exception:
            failure = NodeCommandUncertain if method == "POST" else NodeCommandUnavailable
            raise failure("NODE_COMMAND_TRANSPORT_FAILED") from None
        finally:
            if connection:
                connection.close()
