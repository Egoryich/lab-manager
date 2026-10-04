import asyncio
import hashlib
import json
import uuid

from lab_node_agent.service import SnapshotService


class TransportReader:
    def __init__(self, header, body=b""):
        self.header = header
        self.body = body

    async def readuntil(self, delimiter):
        assert delimiter == b"\r\n\r\n"
        return self.header

    async def readexactly(self, length):
        if len(self.body) != length:
            raise asyncio.IncompleteReadError(self.body, length)
        return self.body


class TLS:
    def getpeercert(self, binary_form):
        assert binary_form
        return b"client-cert"


class TransportWriter:
    def __init__(self):
        self.output = b""

    def get_extra_info(self, key):
        return TLS() if key == "ssl_object" else None

    def write(self, data):
        self.output += data

    async def drain(self):
        pass

    def close(self):
        pass

    async def wait_closed(self):
        pass


class Dispatcher:
    def __init__(self):
        self.calls = []

    def submit(self, body):
        self.calls.append(("submit", body))
        return {"state": "SUBMITTED"}

    def status(self, operation_id):
        self.calls.append(("status", operation_id))
        return {"state": "SUBMITTED"}


def request(service, header, body=b""):
    reader = TransportReader(header, body)
    writer = TransportWriter()
    asyncio.run(service.handle(reader, writer))
    return writer.output


def service(dispatcher=None, segment_client=None):
    return SnapshotService(
        None,
        str(uuid.uuid4()),
        hashlib.sha256(b"client-cert").hexdigest(),
        dispatcher,
        segment_client,
    )


def test_command_http_requires_bounded_json_and_enabled_dispatcher():
    dispatcher = Dispatcher()
    subject = service(dispatcher)
    body = json.dumps({"test": True}).encode()
    header = (
        b"POST /v1/commands HTTP/1.1\r\n"
        + f"Content-Length: {len(body)}\r\n".encode()
        + b"Content-Type: application/json\r\n\r\n"
    )
    result = request(subject, header, body)
    assert result.startswith(b"HTTP/1.1 202 Accepted")
    assert dispatcher.calls == [("submit", body)]

    disabled = request(service(), header, body)
    assert disabled.startswith(b"HTTP/1.1 503 Service Unavailable")
    malformed = request(subject, b"POST /v1/commands HTTP/1.1\r\n\r\n")
    assert malformed.startswith(b"HTTP/1.1 400 Bad Request")
    too_large = request(
        subject,
        b"POST /v1/commands HTTP/1.1\r\nContent-Length: 16385\r\n"
        b"Content-Type: application/json\r\n\r\n",
    )
    assert too_large.startswith(b"HTTP/1.1 400 Bad Request")
    assert dispatcher.calls == [("submit", body)]


def test_command_status_and_get_body_rejection():
    dispatcher = Dispatcher()
    subject = service(dispatcher)
    operation_id = uuid.uuid4()
    response = request(subject, f"GET /v1/commands/{operation_id} HTTP/1.1\r\n\r\n".encode())
    assert response.startswith(b"HTTP/1.1 200 OK")
    assert dispatcher.calls == [("status", operation_id)]
    response = request(
        subject,
        f"GET /v1/commands/{operation_id} HTTP/1.1\r\nContent-Length: 1\r\n\r\n".encode(),
    )
    assert response.startswith(b"HTTP/1.1 400 Bad Request")
    assert dispatcher.calls == [("status", operation_id)]


class Segments:
    def __init__(self):
        self.calls = []

    def create(self, value):
        self.calls.append(("create", value))
        return {**value, "bridge": "lmbr123456", "state": "CREATED"}

    def get(self, allocation_id):
        self.calls.append(("get", allocation_id))
        return {"allocation_id": str(allocation_id), "bridge": "lmbr123456", "state": "CREATED"}


def test_segment_http_is_disabled_without_local_helper_and_uses_bounded_body():
    segments = Segments()
    allocation_id = uuid.uuid4()
    value = {
        "allocation_id": str(allocation_id),
        "mode": "ISOLATED",
        "cidr": "10.70.1.0/29",
    }
    body = json.dumps(value).encode()
    header = (
        b"POST /v1/segments HTTP/1.1\r\n"
        + f"Content-Length: {len(body)}\r\n".encode()
        + b"Content-Type: application/json\r\n\r\n"
    )
    assert request(service(segment_client=segments), header, body).startswith(
        b"HTTP/1.1 202 Accepted"
    )
    assert segments.calls == [("create", value)]
    assert request(service(), header, body).startswith(b"HTTP/1.1 503 Service Unavailable")
    get = f"GET /v1/segments/{allocation_id} HTTP/1.1\r\n\r\n".encode()
    assert request(service(segment_client=segments), get).startswith(b"HTTP/1.1 200 OK")
    assert segments.calls[-1] == ("get", allocation_id)
    oversized = request(
        service(segment_client=segments),
        b"POST /v1/segments HTTP/1.1\r\nContent-Length: 16385\r\n"
        b"Content-Type: application/json\r\n\r\n",
    )
    assert oversized.startswith(b"HTTP/1.1 400 Bad Request")
