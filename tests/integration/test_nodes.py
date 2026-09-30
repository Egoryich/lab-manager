import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from lab_manager.node_transport import NodeTransportError, validate_observation
from lab_manager.nodes import store_observation

pytestmark = pytest.mark.integration


def sample(endpoint, now):
    return {
        "node_id": str(endpoint.id),
        "agent_boot_id": str(uuid.uuid4()),
        "sample": {
            "node": endpoint.node,
            "protocol_version": 1,
            "admission_ready": False,
            "snapshot_id": str(uuid.uuid4()),
            "sample_started_at": now.isoformat(),
            "sample_finished_at": now.isoformat(),
            "storages": [
                {
                    "name": "student-lvm",
                    "backend": "lvmthin",
                    "active": True,
                    "total_bytes": 489970204672,
                    "used_bytes": 1004821,
                    "available_bytes": 477481706 * 1024,
                    "thin_metadata_percent": 0.38,
                }
            ],
            "local_thin_pools": [
                {"storage": "student-lvm", "volumes": [], "ownership_reconciled": False}
            ],
            "guests": [],
            "limitations": ["DISK_COMMITMENTS_NOT_RECONCILED"],
            "host": {
                "logical_cpus": 24,
                "memory_total_bytes": 100,
                "memory_used_bytes": 20,
                "memory_free_bytes": 80,
                "cores_reported": 12,
                "sockets": 1,
                "uptime_seconds": 50,
            },
        },
    }


async def test_node_privacy_failure_preserves_snapshot_and_ordering(
    app, seed, session, client_factory
):
    endpoint = SimpleNamespace(id=uuid.uuid4(), name="Test node", node="pve")
    now = datetime.now(UTC)
    payload, sampled = validate_observation(sample(endpoint, now), endpoint, now)
    await store_observation(app.state.sessions, endpoint, now, payload=payload, sampled_at=sampled)
    async with client_factory() as admin, client_factory() as teacher, client_factory() as anon:
        await session(admin, await seed("ADMIN"))
        await session(teacher, await seed("TEACHER"))
        assert (await anon.get("/api/admin/nodes")).status_code == 401
        assert (await teacher.get("/api/admin/nodes")).status_code == 403
        row = (await admin.get("/api/admin/nodes")).json()[0]
        assert row["status"] == "FRESH" and row["host"]["logical_cpus"] == 24
        assert row["storages"][0]["thin_metadata_percent"] == 0.38
        assert row["storages"][0]["observed_volume_count"] == 0
        assert row["admission_ready"] is False
        await store_observation(
            app.state.sessions, endpoint, now + timedelta(seconds=1), error="NODE_CONNECTION_FAILED"
        )
        # A slow older success must not overwrite the newer failed attempt.
        await store_observation(
            app.state.sessions, endpoint, now, payload=payload, sampled_at=sampled
        )
        row = (await admin.get("/api/admin/nodes")).json()[0]
        assert row["status"] == "STALE" and row["host"]["logical_cpus"] == 24
        assert row["error_code"] == "NODE_CONNECTION_FAILED"
        old = now - timedelta(minutes=3)
        old_payload = sample(endpoint, old)
        await store_observation(
            app.state.sessions,
            endpoint,
            now + timedelta(seconds=2),
            payload=old_payload,
            sampled_at=old,
        )
        assert (await admin.get("/api/admin/nodes")).json()[0]["status"] == "STALE"


async def test_reject_wrong_identity_stale_and_naive_sample():
    endpoint = SimpleNamespace(id=uuid.uuid4(), node="pve")
    now = datetime.now(UTC)
    data = sample(endpoint, now)
    data["node_id"] = str(uuid.uuid4())
    with pytest.raises(NodeTransportError):
        validate_observation(data, endpoint, now)
    for date in (now - timedelta(minutes=3), now + timedelta(minutes=1), now.replace(tzinfo=None)):
        with pytest.raises(NodeTransportError):
            validate_observation(sample(endpoint, date), endpoint, now)
