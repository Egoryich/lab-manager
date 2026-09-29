"""Exercise the VPS client against the real agent TLS endpoint on loopback."""

import asyncio
import importlib.util
import sys
import uuid
from dataclasses import replace
from pathlib import Path

import pytest
from lab_manager.node_transport import NodeEndpoint, NodeTransportError, fetch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps/node-agent/src"))
sys.path.insert(0, str(ROOT / "apps/node-agent/tests"))
from lab_node_agent.service import SnapshotService, server_context  # noqa: E402
from test_inventory import Reader  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "transport_test_certificates", ROOT / "apps/node-agent/tests/test_service.py"
)
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)


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
