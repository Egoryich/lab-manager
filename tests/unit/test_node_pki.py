"""The distributed public bundle must verify without transporting private keys."""

import importlib.util
import json
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or not shutil.which("openssl"),
    reason="OpenSSL provisioning is executed on Linux hosts",
)


def test_transport_pki_keys_stay_on_origin_and_server_has_tailnet_san(tmp_path):
    path = Path(__file__).resolve().parents[2] / "tools/node-pki.py"
    spec = importlib.util.spec_from_file_location("node_pki", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    node, control = tmp_path / "node", tmp_path / "control"
    node.mkdir()
    control.mkdir()
    module.initialize_node(node)
    module.initialize_control(control)
    node_id = (node / "node-id").read_text().strip()
    assert uuid.UUID(node_id)
    module.sign_node(control, node / "server.csr", node_id, "100.64.0.2")
    bundle = json.loads((control / node_id / "public-bundle.json").read_text())
    assert bundle["node_id"] == node_id
    assert bundle["address"] == "100.64.0.2"
    assert "PRIVATE KEY" not in json.dumps(bundle)
    assert (node / "server.key").is_file()
    assert not (control / "server.key").exists()
    assert (control / "client.key").is_file()
    assert not (node / "client.key").exists()
    subprocess.run(
        [
            "openssl",
            "verify",
            "-CAfile",
            str(control / "ca.pem"),
            "-verify_ip",
            "100.64.0.2",
            str(control / node_id / "server.pem"),
        ],
        check=True,
        capture_output=True,
    )
