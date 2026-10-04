import json
import socket
import threading
import uuid

import pytest

from lab_node_agent.segment_client import SegmentClient
from lab_node_agent.segments import SegmentSpec


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
            for _ in range(2):
                connection, _ = listener.accept()
                with connection:
                    data = bytearray()
                    while not data.endswith(b"\n"):
                        data.extend(connection.recv(1024))
                    requests.append(json.loads(data))
                    connection.sendall(json.dumps({"ok": True, "result": record}).encode() + b"\n")

        thread = threading.Thread(target=serve)
        thread.start()
        client = SegmentClient(path)
        assert client.create(spec) == record
        assert client.get(allocation_id) == record
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert requests == [
        {"action": "create", "spec": spec},
        {"action": "get", "allocation_id": str(allocation_id)},
    ]
