import json
import socket
import threading
import uuid

import pytest

from lab_node_agent.segment_client import SegmentClient
from lab_node_agent.segments import SegmentSpec


def test_gateway_client_checks_exact_owned_response(monkeypatch):
    allocation_id = uuid.uuid4()
    spec = SegmentSpec.parse(
        {"allocation_id": str(allocation_id), "mode": "ISOLATED", "cidr": "10.70.8.0/30"}
    )
    client = SegmentClient()
    requests = []

    def exchange(request):
        requests.append(request)
        if request["action"] == "get":
            return {**spec.record(), "state": "CREATED"}
        if request["action"] == "prepare_gateway":
            return {**spec.record(), "gateway": spec.gateway, "state": "GATEWAY_PREPARED"}
        return {**spec.record(), "state": "CREATED"}

    monkeypatch.setattr(client, "_exchange", exchange)
    assert client.prepare_gateway(allocation_id)["gateway"] == "10.70.8.1/30"
    assert client.close_gateway(allocation_id)["state"] == "CREATED"
    assert [item["action"] for item in requests] == [
        "get",
        "prepare_gateway",
        "get",
        "close_gateway",
    ]


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Unix sockets required")
def test_unprivileged_client_exchanges_exact_segment_identity(tmp_path):
    allocation_id = uuid.uuid4()
    spec = {
        "allocation_id": str(allocation_id),
        "mode": "GROUP_LAN",
        "cidr": "10.70.2.0/29",
    }
    record = {**SegmentSpec.parse(spec).record(), "state": "CREATED"}
    path = tmp_path / "segments.sock"
    requests = []
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(path))
        listener.listen(2)

        def serve():
            for _ in range(3):
                connection, _ = listener.accept()
                with connection:
                    data = bytearray()
                    while not data.endswith(b"\n"):
                        data.extend(connection.recv(1024))
                    requests.append(json.loads(data))
                    response = record if len(requests) < 3 else {**record, "state": "ACTIVE"}
                    connection.sendall(
                        json.dumps({"ok": True, "result": response}).encode() + b"\n"
                    )

        thread = threading.Thread(target=serve)
        thread.start()
        client = SegmentClient(path)
        assert client.create(spec) == record
        assert client.get(allocation_id) == record
        assert client.get(allocation_id)["state"] == "ACTIVE"
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert requests == [
        {"action": "create", "spec": spec},
        {"action": "get", "allocation_id": str(allocation_id)},
        {"action": "get", "allocation_id": str(allocation_id)},
    ]
