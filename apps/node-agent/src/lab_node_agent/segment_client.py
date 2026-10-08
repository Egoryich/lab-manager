"""Unprivileged client for the local, root-owned segment helper."""

import json
import socket
import uuid
from pathlib import Path

from lab_node_agent.admission import Admission, AdmissionRequest
from lab_node_agent.segment_daemon import MAX_REQUEST, SOCKET
from lab_node_agent.segments import SegmentError, SegmentSpec


class SegmentClient:
    def __init__(self, path: Path = SOCKET):
        self.path = path

    def _exchange(self, request: dict) -> dict:
        raw = json.dumps(request, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        if len(raw) > MAX_REQUEST:
            raise SegmentError("INVALID_SEGMENT_REQUEST")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(4)
                connection.connect(str(self.path))
                connection.sendall(raw)
                result = bytearray()
                while len(result) <= MAX_REQUEST:
                    chunk = connection.recv(min(1024, MAX_REQUEST + 1 - len(result)))
                    if not chunk:
                        break
                    result.extend(chunk)
                    if b"\n" in chunk:
                        break
            if len(result) > MAX_REQUEST or not result.endswith(b"\n") or result.count(b"\n") != 1:
                raise ValueError("response framing")
            response = json.loads(result[:-1])
            if not isinstance(response, dict) or set(response) not in (
                {"ok", "result"},
                {"ok", "error"},
            ):
                raise ValueError("response shape")
            if response["ok"] is True and isinstance(response.get("result"), dict):
                return response["result"]
            if response["ok"] is False and isinstance(response.get("error"), str):
                raise SegmentError(response["error"])
            raise ValueError("response values")
        except SegmentError:
            raise
        except (OSError, ValueError, TypeError) as error:
            raise SegmentError("SEGMENT_HELPER_UNAVAILABLE") from error

    def create(self, value: dict) -> dict:
        spec = SegmentSpec.parse(value)
        result = self._exchange(
            {
                "action": "create",
                "spec": {
                    "allocation_id": str(spec.allocation_id),
                    "mode": spec.mode.value,
                    "cidr": spec.cidr,
                },
            }
        )
        if result != {**spec.record(), "state": "CREATED"}:
            raise SegmentError("SEGMENT_HELPER_MISMATCH")
        return result

    def get(self, allocation_id: uuid.UUID) -> dict:
        if not isinstance(allocation_id, uuid.UUID) or allocation_id.int == 0:
            raise SegmentError("INVALID_SEGMENT_ID")
        result = self._exchange({"action": "get", "allocation_id": str(allocation_id)})
        try:
            spec = SegmentSpec.parse(
                {field: result[field] for field in ("allocation_id", "mode", "cidr")}
            )
        except (KeyError, TypeError, SegmentError) as error:
            raise SegmentError("SEGMENT_HELPER_MISMATCH") from error
        if (
            spec.allocation_id != allocation_id
            or result.get("bridge") != spec.bridge
            or result.get("state") not in ("CREATED", "ACTIVE")
        ):
            raise SegmentError("SEGMENT_HELPER_MISMATCH")
        return result

    def prepare_gateway(self, allocation_id: uuid.UUID) -> dict:
        current = self.get(allocation_id)
        result = self._exchange({"action": "prepare_gateway", "allocation_id": str(allocation_id)})
        spec = SegmentSpec.parse(
            {field: current[field] for field in ("allocation_id", "mode", "cidr")}
        )
        if result != {**spec.record(), "gateway": spec.gateway, "state": "GATEWAY_PREPARED"}:
            raise SegmentError("SEGMENT_HELPER_MISMATCH")
        return result

    def close_gateway(self, allocation_id: uuid.UUID) -> dict:
        current = self.get(allocation_id)
        result = self._exchange({"action": "close_gateway", "allocation_id": str(allocation_id)})
        spec = SegmentSpec.parse(
            {field: current[field] for field in ("allocation_id", "mode", "cidr")}
        )
        if result != {**spec.record(), "state": "CREATED"}:
            raise SegmentError("SEGMENT_HELPER_MISMATCH")
        return result

    def admit_ssh(self, value: dict) -> dict:
        request = AdmissionRequest.parse(value)
        result = self._exchange({"action": "admit_ssh", "admission": request.record()})
        if (
            not isinstance(result, dict)
            or set(result) != set(request.record()) | {"mac", "state"}
            or any(result.get(key) != item for key, item in request.record().items())
            or result.get("state") != "APPLIED"
        ):
            raise SegmentError("SEGMENT_HELPER_MISMATCH")
        try:
            Admission.parse({key: value for key, value in result.items() if key != "state"})
        except SegmentError as error:
            raise SegmentError("SEGMENT_HELPER_MISMATCH") from error
        return result

    def revoke_ssh(self, allocation_id: uuid.UUID) -> dict:
        if not isinstance(allocation_id, uuid.UUID) or allocation_id.int == 0:
            raise SegmentError("INVALID_SEGMENT_ID")
        result = self._exchange({"action": "revoke_ssh", "allocation_id": str(allocation_id)})
        if result not in (
            {"allocation_id": str(allocation_id), "state": "REVOKED"},
            {"allocation_id": str(allocation_id), "state": "ABSENT"},
        ):
            raise SegmentError("SEGMENT_HELPER_MISMATCH")
        return result
