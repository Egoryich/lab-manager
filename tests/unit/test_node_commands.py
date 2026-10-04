import hashlib
import json
import ssl
import uuid

import pytest
from lab_manager.node_commands import (
    NodeCommandClient,
    NodeCommandRejected,
    NodeCommandUnavailable,
    NodeCommandUncertain,
)
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
    def __init__(self, status, body):
        self.status = status
        self.body = json.dumps(body).encode()

    def read(self, limit):
        return self.body[:limit]


class Connection:
    def __init__(self, response, *, fail=False):
        self.response = response
        self.fail = fail
        self.sock = Socket()
        self.requests = []
        self.closed = False

    def connect(self):
        pass

    def request(self, method, path, body=None, headers=None):
        self.requests.append((method, path, body, headers))
        if self.fail:
            raise TimeoutError("lost after send")

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


def make_client(tmp_path, monkeypatch, response, *, fail=False):
    node_id = uuid.uuid4()
    for name in ("ca.pem", "client.pem", "client.key"):
        (tmp_path / name).write_text("test")
    endpoint = NodeEndpoint(
        node_id,
        "Lab",
        "https://100.64.0.2:18443",
        "pve",
        hashlib.sha256(b"server-cert").hexdigest(),
        tmp_path / "ca.pem",
        tmp_path / "client.pem",
        tmp_path / "client.key",
    )
    connection = Connection(response, fail=fail)
    monkeypatch.setattr(ssl, "create_default_context", lambda **kwargs: Context())
    monkeypatch.setattr(
        "lab_manager.node_commands.http.client.HTTPSConnection",
        lambda *args, **kwargs: connection,
    )
    return NodeCommandClient(endpoint), connection


def command(node_id):
    return {
        "operation_id": str(uuid.uuid4()),
        "node_id": str(node_id),
        "kind": "LXC_START",
        "runtime_id": str(uuid.uuid4()),
        "generation": 1,
        "vmid": 901001,
    }


def receipt(request, *, state="SUBMITTED"):
    return {
        "operation_id": request["operation_id"],
        "kind": request["kind"],
        "vmid": request["vmid"],
        "runtime_id": request["runtime_id"],
        "generation": request["generation"],
        "state": state,
        "task_id": "UPID",
        "error_code": None,
    }


def test_submit_pins_tls_and_returns_matching_receipt(tmp_path, monkeypatch):
    placeholder = Response(202, {})
    client, connection = make_client(tmp_path, monkeypatch, placeholder)
    request = command(client.endpoint.id)
    connection.response = Response(202, receipt(request))
    assert client.submit(request)["state"] == "SUBMITTED"
    assert len(connection.requests) == 1
    method, path, body, headers = connection.requests[0]
    assert (method, path) == ("POST", "/v1/commands")
    assert json.loads(body)["node_id"] == str(client.endpoint.id)
    assert headers["Content-Type"] == "application/json"
    assert connection.closed


def test_lost_post_response_is_uncertain_and_never_retried(tmp_path, monkeypatch):
    client, connection = make_client(tmp_path, monkeypatch, Response(202, {}), fail=True)
    with pytest.raises(NodeCommandUncertain, match="NODE_COMMAND_TRANSPORT_FAILED"):
        client.submit(command(client.endpoint.id))
    assert len(connection.requests) == 1


def test_mismatched_receipt_or_server_error_is_uncertain(tmp_path, monkeypatch):
    client, connection = make_client(tmp_path, monkeypatch, Response(202, {}))
    request = command(client.endpoint.id)
    wrong = receipt(request)
    wrong["vmid"] = 901002
    connection.response = Response(202, wrong)
    with pytest.raises(NodeCommandUncertain, match="NODE_COMMAND_RECEIPT_MISMATCH"):
        client.submit(request)
    connection.response = Response(503, {"error": "COMMANDS_DISABLED"})
    with pytest.raises(NodeCommandUncertain, match="NODE_COMMAND_TRANSPORT_FAILED"):
        client.submit(request)


def test_status_is_read_only_and_errors_are_classified(tmp_path, monkeypatch):
    client, connection = make_client(tmp_path, monkeypatch, Response(200, {}))
    request = command(client.endpoint.id)
    connection.response = Response(200, receipt(request))
    assert client.status(uuid.UUID(request["operation_id"]))["state"] == "SUBMITTED"
    assert connection.requests[-1][0:2] == (
        "GET",
        f"/v1/commands/{request['operation_id']}",
    )
    connection.response = Response(404, {"error": "OPERATION_NOT_FOUND"})
    with pytest.raises(NodeCommandRejected, match="OPERATION_NOT_FOUND"):
        client.status(uuid.UUID(request["operation_id"]))
    connection.fail = True
    with pytest.raises(NodeCommandUnavailable, match="NODE_COMMAND_TRANSPORT_FAILED"):
        client.status(uuid.UUID(request["operation_id"]))


def test_rejects_foreign_target_without_network_call(tmp_path, monkeypatch):
    client, connection = make_client(tmp_path, monkeypatch, Response(202, {}))
    request = command(uuid.uuid4())
    with pytest.raises(NodeCommandRejected, match="INVALID_COMMAND"):
        client.submit(request)
    assert not connection.requests
