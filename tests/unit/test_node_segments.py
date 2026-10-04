import hashlib
import json
import ssl
import uuid

import pytest
from lab_manager.node_segments import NodeSegmentClient, NodeSegmentError, bridge_name
from lab_manager.node_transport import NodeEndpoint


class Context:
    def load_cert_chain(self, certificate, key):
        assert certificate.endswith("client.pem")
        assert key.endswith("client.key")


class Socket:
    def getpeercert(self, binary_form):
        assert binary_form
        return b"server-cert"


class Response:
    def __init__(self, status, data):
        self.status = status
        self.data = json.dumps(data).encode()

    def read(self, limit):
        return self.data[:limit]


class Connection:
    def __init__(self, response):
        self.sock = Socket()
        self.response = response
        self.requests = []

    def connect(self):
        pass

    def request(self, method, path, body=None, headers=None):
        self.requests.append((method, path, body, headers))

    def getresponse(self):
        return self.response

    def close(self):
        pass


def test_pinned_segment_create_and_status(tmp_path, monkeypatch):
    for name in ("ca.pem", "client.pem", "client.key"):
        (tmp_path / name).write_text("test")
    endpoint = NodeEndpoint(
        uuid.uuid4(),
        "Lab",
        "https://100.64.0.2:18443",
        "pve",
        hashlib.sha256(b"server-cert").hexdigest(),
        tmp_path / "ca.pem",
        tmp_path / "client.pem",
        tmp_path / "client.key",
    )
    allocation_id = uuid.uuid4()
    value = {
        "allocation_id": str(allocation_id),
        "mode": "ISOLATED",
        "cidr": "10.70.1.0/29",
        "bridge": bridge_name(allocation_id),
        "state": "CREATED",
    }
    connection = Connection(Response(202, value))
    monkeypatch.setattr(ssl, "create_default_context", lambda **kwargs: Context())
    monkeypatch.setattr(
        "lab_manager.node_segments.http.client.HTTPSConnection",
        lambda *args, **kwargs: connection,
    )
    client = NodeSegmentClient(endpoint)
    assert client.create(allocation_id, "ISOLATED", "10.70.1.0/29") == value
    assert connection.requests[-1][0:2] == ("POST", "/v1/segments")
    assert json.loads(connection.requests[-1][2])["allocation_id"] == str(allocation_id)
    connection.response = Response(200, value)
    assert client.status(allocation_id) == value
    assert connection.requests[-1][0:2] == ("GET", f"/v1/segments/{allocation_id}")
    connection.response = Response(404, {"error": "SEGMENT_NOT_FOUND"})
    assert client.status(uuid.uuid4()) is None
    connection.response = Response(202, {**value, "bridge": "vmbr0"})
    with pytest.raises(NodeSegmentError, match="SEGMENT_RESPONSE_MISMATCH"):
        client.create(allocation_id, "ISOLATED", "10.70.1.0/29")
