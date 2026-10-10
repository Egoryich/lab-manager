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
    connection.response = Response(200, {**value, "state": "ACTIVE"})
    assert client.status(allocation_id)["state"] == "ACTIVE"
    connection.response = Response(404, {"error": "SEGMENT_NOT_FOUND"})
    assert client.status(uuid.uuid4()) is None
    connection.response = Response(202, {**value, "bridge": "vmbr0"})
    with pytest.raises(NodeSegmentError, match="SEGMENT_RESPONSE_MISMATCH"):
        client.create(allocation_id, "ISOLATED", "10.70.1.0/29")


def test_pinned_ssh_admission_and_revoke(tmp_path, monkeypatch):
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
    admission = {
        "allocation_id": str(allocation_id),
        "runtime_id": str(uuid.uuid4()),
        "generation": 1,
        "vmid": 901001,
        "address": "10.70.1.2",
    }
    connection = Connection(
        Response(200, {**admission, "mac": "bc:24:11:aa:bb:cc", "state": "APPLIED"})
    )
    monkeypatch.setattr(ssl, "create_default_context", lambda **kwargs: Context())
    monkeypatch.setattr(
        "lab_manager.node_segments.http.client.HTTPSConnection",
        lambda *args, **kwargs: connection,
    )
    client = NodeSegmentClient(endpoint)
    assert client.admit_ssh(admission)["state"] == "APPLIED"
    assert connection.requests[-1][0:2] == ("POST", "/v1/ssh-admissions")
    connection.response = Response(200, {"allocation_id": str(allocation_id), "state": "REVOKED"})
    assert client.revoke_ssh(allocation_id)["state"] == "REVOKED"
    assert connection.requests[-1][0:2] == ("DELETE", f"/v1/ssh-admissions/{allocation_id}")
    connection.response = Response(200, {"allocation_id": str(allocation_id), "state": "APPLIED"})
    with pytest.raises(NodeSegmentError, match="SSH_ADMISSION_RESPONSE_MISMATCH"):
        client.revoke_ssh(allocation_id)
    with pytest.raises(NodeSegmentError, match="INVALID_SSH_ADMISSION"):
        client.admit_ssh({**admission, "address": "192.168.0.1"})


def test_gateway_lifecycle_requires_exact_node_response(tmp_path, monkeypatch):
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
        "cidr": "10.70.1.0/30",
        "bridge": bridge_name(allocation_id),
        "gateway": "10.70.1.1/30",
        "state": "GATEWAY_PREPARED",
    }
    connection = Connection(Response(200, value))
    monkeypatch.setattr(ssl, "create_default_context", lambda **kwargs: Context())
    monkeypatch.setattr(
        "lab_manager.node_segments.http.client.HTTPSConnection",
        lambda *args, **kwargs: connection,
    )
    client = NodeSegmentClient(endpoint)
    assert client.prepare_gateway(allocation_id, "ISOLATED", "10.70.1.0/30") == value
    assert connection.requests[-1][0:2] == ("PUT", f"/v1/segments/{allocation_id}/gateway")
    connection.response = Response(200, {**value, "gateway": "10.70.1.1/29"})
    with pytest.raises(NodeSegmentError, match="SEGMENT_RESPONSE_MISMATCH"):
        client.prepare_gateway(allocation_id, "ISOLATED", "10.70.1.0/30")
    connection.response = Response(
        200, {key: item for key, item in value.items() if key != "gateway"} | {"state": "CREATED"}
    )
    assert client.close_gateway(allocation_id, "ISOLATED", "10.70.1.0/30")["state"] == "CREATED"
    assert connection.requests[-1][0:2] == ("DELETE", f"/v1/segments/{allocation_id}/gateway")
    with pytest.raises(NodeSegmentError, match="INVALID_SEGMENT_SPEC"):
        client.prepare_gateway(allocation_id, "ISOLATED", "192.168.0.0/30")
