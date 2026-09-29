import json
import ssl
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from lab_node_agent.cli import main, read_credentials
from lab_node_agent.inventory import collect
from lab_node_agent.proxmox import InventoryError, ProxmoxConfig, ProxmoxReader


def data():
    return {
        "permissions": {"/": {"Sys.Audit": 1, "VM.Audit": 1, "Datastore.Audit": 1}},
        "version": {"version": "9.2.0"},
        "status": {
            "memory": {"total": 32 * 2**30, "used": 2**30, "free": 31 * 2**30},
            "cpuinfo": {"cpus": 24, "cores": 12, "sockets": 1},
            "uptime": 50,
        },
        "storage": [
            {
                "storage": "student-lvm",
                "type": "lvmthin",
                "active": 1,
                "total": 456 * 2**30,
                "used": 2**30,
                "avail": 455 * 2**30,
            }
        ],
        "qemu": [],
        "lxc": [
            {
                "vmid": 201,
                "name": "external-test",
                "status": "stopped",
                "maxmem": 2**30,
                "cpus": 1,
                "maxdisk": 10 * 2**30,
            }
        ],
    }


class Reader:
    config = SimpleNamespace(node="pve")

    def __init__(self):
        self.data = data()

    def get(self, key):
        return self.data[key]


def test_snapshot_preserves_external_guests_without_admission():
    result = collect(Reader())
    assert result["host"]["memory_total_bytes"] == 32 * 2**30
    assert result["guests"][0]["ownership"] == "UNVERIFIED"
    assert result["guests"][0]["reported_maxdisk_bytes"] == 10 * 2**30
    assert result["guests"][0]["reported_status"] == "stopped"
    assert result["storages"][0]["thin_metadata_percent"] is None
    assert result["admission_ready"] is False
    assert "token" not in json.dumps(result)


def test_missing_capacity_is_unknown_and_duplicate_guest_is_rejected():
    reader = Reader()
    reader.data["storage"][0].pop("avail")
    assert collect(reader)["storages"][0]["available_bytes"] is None
    reader.data["qemu"] = list(reader.data["lxc"])
    with pytest.raises(InventoryError, match="INCONSISTENT_GUEST_LIST"):
        collect(reader)


def test_narrow_token_cannot_present_empty_inventory_as_free_capacity():
    reader = Reader()
    reader.data["permissions"]["/"].pop("VM.Audit")
    with pytest.raises(InventoryError, match="GLOBAL_AUDIT_PERMISSIONS_REQUIRED"):
        collect(reader)


@pytest.fixture
def https_pve(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
        .sign(key, hashes.SHA256())
    )
    ca = tmp_path / "ca.pem"
    ca.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    private = tmp_path / "key.pem"
    private.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    state = SimpleNamespace(requests=[], redirect=False, denied=False, oversized=False)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            state.requests.append((self.command, self.path, self.headers.get("Authorization")))
            if state.redirect:
                self.send_response(302)
                self.send_header("Location", "https://example.invalid/stolen")
                self.end_headers()
                return
            if state.denied:
                self.send_response(403)
                self.end_headers()
                self.wfile.write(b"secret-provider-error")
                return
            self.send_response(200)
            self.end_headers()
            resource = self.path.split("/")[-1].split("?")[0]
            payload = (
                b"x" * 1025 if state.oversized else json.dumps({"data": data()[resource]}).encode()
            )
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(ca, private)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = ProxmoxConfig(
        f"https://localhost:{server.server_port}",
        "pve",
        ca,
        "inventory@pve!reader",
        "test-token-value-0123456789",
    )
    try:
        yield config, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_verified_https_allows_only_fixed_gets_and_refuses_redirect(https_pve, monkeypatch):
    config, state = https_pve
    monkeypatch.setenv("HTTPS_PROXY", "http://invalid.example:9")
    reader = ProxmoxReader(config)
    assert collect(reader)["node"] == "pve"
    assert len(state.requests) == 6
    assert all(method == "GET" for method, _, _ in state.requests)
    assert all(
        auth == "PVEAPIToken=inventory@pve!reader=test-token-value-0123456789"
        for _, _, auth in state.requests
    )
    assert config.token_secret not in repr(config)
    with pytest.raises(InventoryError, match="RESOURCE_NOT_ALLOWED"):
        reader.get("../../status/start")
    state.redirect = True
    with pytest.raises(InventoryError, match="PROXMOX_REDIRECT_REFUSED"):
        reader.get("status")


def test_certificate_hostname_is_verified(https_pve):
    config, _ = https_pve
    wrong = ProxmoxConfig(
        config.origin.replace("localhost", "127.0.0.1"),
        config.node,
        config.ca_file,
        config.token_id,
        config.token_secret,
    )
    with pytest.raises(InventoryError, match="PROXMOX_CONNECTION_FAILED"):
        ProxmoxReader(wrong).get("status")


def test_size_bound_and_error_redaction(https_pve, monkeypatch):
    from lab_node_agent import proxmox

    config, state = https_pve
    reader = ProxmoxReader(config)
    state.denied = True
    with pytest.raises(InventoryError, match="^PROXMOX_ACCESS_DENIED$"):
        reader.get("status")
    state.denied = False
    state.oversized = True
    monkeypatch.setattr(proxmox, "MAX_RESPONSE_BYTES", 1024)
    with pytest.raises(InventoryError, match="PROXMOX_RESPONSE_TOO_LARGE"):
        reader.get("status")


def test_cli_does_not_echo_invalid_secret(tmp_path, monkeypatch, capsys):
    token = tmp_path / "token.json"
    token.write_text('{"token_id":"bad","token_secret":"secret-must-not-leak"}')
    token.chmod(0o600)
    monkeypatch.setattr(
        "sys.argv",
        [
            "lab-node-agent",
            "--origin",
            "http://localhost:8006",
            "--node",
            "pve",
            "--ca-file",
            str(tmp_path / "ca"),
            "--credentials-file",
            str(token),
        ],
    )
    assert main() == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert "secret-must-not-leak" not in captured.err
    assert json.loads(captured.err)["error"] == "INVALID_PROXMOX_CONFIGURATION"


def test_credentials_file_schema(tmp_path):
    token = tmp_path / "token.json"
    token.write_text('{"token_id":"example","token_secret":"secret","extra":true}')
    token.chmod(0o600)
    with pytest.raises(InventoryError, match="INVALID_CREDENTIAL_FILE"):
        read_credentials(token)
