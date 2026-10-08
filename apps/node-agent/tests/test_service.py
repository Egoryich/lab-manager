import asyncio
import hashlib
import json
import ssl
import time
import uuid
from datetime import UTC, datetime, timedelta

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from test_inventory import Reader

from lab_node_agent.service import SnapshotService, server_context


def certificates(path):
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test transport CA")])
    now = datetime.now(UTC)
    ca = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(False, False, False, False, False, True, True, False, False), True
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), False)
        .sign(ca_key, hashes.SHA256())
    )
    (path / "ca.pem").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    fingerprints = {}
    for kind in ("server", "client", "other"):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, kind)])
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), False
            )
            .add_extension(
                x509.KeyUsage(True, False, True, False, False, False, False, False, False), True
            )
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), False)
            .add_extension(
                x509.ExtendedKeyUsage(
                    [
                        ExtendedKeyUsageOID.SERVER_AUTH
                        if kind == "server"
                        else ExtendedKeyUsageOID.CLIENT_AUTH
                    ]
                ),
                False,
            )
            .sign(ca_key, hashes.SHA256())
        )
        (path / f"{kind}.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        (path / f"{kind}.key").write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        fingerprints[kind] = hashlib.sha256(
            cert.public_bytes(serialization.Encoding.DER)
        ).hexdigest()
    return fingerprints


def test_mtls_snapshot_boundary_and_freshness(tmp_path):
    pins = certificates(tmp_path)

    async def scenario():
        service = SnapshotService(Reader(), str(uuid.uuid4()), pins["client"])
        await service.refresh()
        tls = server_context(tmp_path / "ca.pem", tmp_path / "server.pem", tmp_path / "server.key")
        server = await asyncio.start_server(service.handle, "127.0.0.1", 0, ssl=tls, limit=8192)
        port = server.sockets[0].getsockname()[1]

        async def request(
            client="client", route=b"GET /v1/inventory HTTP/1.1", hostname="localhost"
        ):
            context = ssl.create_default_context(cafile=str(tmp_path / "ca.pem"))
            if client:
                context.load_cert_chain(tmp_path / f"{client}.pem", tmp_path / f"{client}.key")
            writer = None
            try:
                reader, writer = await asyncio.open_connection(
                    "127.0.0.1", port, ssl=context, server_hostname=hostname
                )
                writer.write(route + b"\r\nHost: localhost\r\n\r\n")
                await writer.drain()
                return await asyncio.wait_for(reader.read(), 5)
            except (OSError, ssl.SSLError):
                if client == "client" and hostname == "localhost":
                    raise
                return b""
            finally:
                if writer:
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except OSError:
                        pass

        async with server:
            response = await request()
            assert response.startswith(b"HTTP/1.1 200")
            payload = json.loads(response.split(b"\r\n\r\n", 1)[1])
            assert payload["node_id"] == service.node_id
            assert payload["sample"]["admission_ready"] is False
            assert not await request(None)
            assert not await request("other")
            assert not await request(hostname="wrong-host")
            assert (await request(route=b"POST /v1/inventory HTTP/1.1")).startswith(b"HTTP/1.1 404")
            service.collected_at = time.monotonic() - 121
            assert (await request()).startswith(b"HTTP/1.1 503")
            service.reader.data["status"] = None
            await service.refresh()
            assert service.body is None
            assert (await request()).startswith(b"HTTP/1.1 503")

    asyncio.run(scenario())


def test_mtls_ssh_admission_only_dispatches_typed_requests(tmp_path):
    pins = certificates(tmp_path)
    allocation_id = uuid.uuid4()
    calls = []

    class Segments:
        def admit_ssh(self, value):
            calls.append(("admit", value))
            return {**value, "state": "APPLIED"}

        def revoke_ssh(self, value):
            calls.append(("revoke", value))
            return {"allocation_id": str(value), "state": "REVOKED"}

    async def scenario():
        service = SnapshotService(
            Reader(), str(uuid.uuid4()), pins["client"], segment_client=Segments()
        )
        tls = server_context(tmp_path / "ca.pem", tmp_path / "server.pem", tmp_path / "server.key")
        server = await asyncio.start_server(service.handle, "127.0.0.1", 0, ssl=tls, limit=8192)
        port = server.sockets[0].getsockname()[1]

        async def request(line, body=None):
            context = ssl.create_default_context(cafile=str(tmp_path / "ca.pem"))
            context.load_cert_chain(tmp_path / "client.pem", tmp_path / "client.key")
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", port, ssl=context, server_hostname="localhost"
            )
            headers = b"Host: localhost\r\n"
            if body is not None:
                headers += (
                    b"Content-Type: application/json\r\nContent-Length: "
                    + str(len(body)).encode()
                    + b"\r\n"
                )
            writer.write(line + b"\r\n" + headers + b"\r\n" + (body or b""))
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), 5)
            writer.close()
            await writer.wait_closed()
            return response

        async with server:
            record = {"allocation_id": str(allocation_id), "vmid": 901001}
            body = json.dumps(record).encode()
            assert (await request(b"POST /v1/ssh-admissions HTTP/1.1", body)).startswith(
                b"HTTP/1.1 200"
            )
            assert (
                await request(
                    b"DELETE /v1/ssh-admissions/" + str(allocation_id).encode() + b" HTTP/1.1"
                )
            ).startswith(b"HTTP/1.1 200")
            assert (await request(b"DELETE /v1/ssh-admissions/nope HTTP/1.1")).startswith(
                b"HTTP/1.1 400"
            )
            assert (await request(b"POST /v1/ssh-admissions HTTP/1.1")).startswith(b"HTTP/1.1 400")
        assert calls == [("admit", record), ("revoke", allocation_id)]

    asyncio.run(scenario())
