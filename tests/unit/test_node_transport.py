"""Exercise the VPS client against the real agent TLS endpoint on loopback."""

import asyncio
import importlib.util
import sys
import uuid
from dataclasses import replace
from pathlib import Path

import pytest
from lab_manager.node_transport import NodeEndpoint, NodeTransportError, fetch, validate_observation

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps/node-agent/src"))
sys.path.insert(0, str(ROOT / "apps/node-agent/tests"))
from lab_node_agent.inventory import collect  # noqa: E402
from lab_node_agent.service import SnapshotService, server_context  # noqa: E402
from test_inventory import Reader  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "transport_test_certificates", ROOT / "apps/node-agent/tests/test_service.py"
)
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)


def test_vps_requires_typed_root_evidence_for_ready_inventory(tmp_path):
    node_id = uuid.uuid4()
    endpoint = NodeEndpoint(
        node_id,
        "Node",
        "https://127.0.0.1:18443",
        "pve",
        "0" * 64,
        tmp_path / "ca",
        tmp_path / "client",
        tmp_path / "key",
    )
    sample = collect(Reader())
    sample["admission_ready"] = True
    sample["network_security"] = {
        "ready": True,
        "guacamole": {"address": "10.60.0.10", "bridge": "vmbr1"},
    }
    payload = {"node_id": str(node_id), "agent_boot_id": str(uuid.uuid4()), "sample": sample}
    validate_observation(payload, endpoint)
    sample["network_security"] = {"ready": False}
    with pytest.raises(NodeTransportError, match="NETWORK_ATTESTATION_INVALID"):
        validate_observation(payload, endpoint)


def test_vps_to_agent_roundtrip_and_server_pin(tmp_path):
    pins = helpers.certificates(tmp_path)

    async def scenario():
        node_id = uuid.uuid4()
        service = SnapshotService(Reader(), str(node_id), pins["client"])
        await service.refresh()
        context = server_context(
            tmp_path / "ca.pem", tmp_path / "server.pem", tmp_path / "server.key"
        )
        server = await asyncio.start_server(service.handle, "127.0.0.1", 0, ssl=context)
        endpoint = NodeEndpoint(
            node_id,
            "Node",
            f"https://localhost:{server.sockets[0].getsockname()[1]}",
            "pve",
            pins["server"],
            tmp_path / "ca.pem",
            tmp_path / "client.pem",
            tmp_path / "client.key",
        )
        async with server:
            result, _ = await asyncio.to_thread(fetch, endpoint)
            assert result["node_id"] == str(node_id)
            assert result["sample"]["host"]["logical_cpus"] == 24
            for wrong in (
                replace(endpoint, server_sha256="0" * 64),
                replace(endpoint, id=uuid.uuid4()),
                replace(endpoint, certificate=tmp_path / "other.pem", key=tmp_path / "other.key"),
            ):
                with pytest.raises(NodeTransportError):
                    await asyncio.to_thread(fetch, wrong)

    asyncio.run(scenario())
