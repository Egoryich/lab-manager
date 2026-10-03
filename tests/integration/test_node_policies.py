import uuid
from datetime import UTC, datetime

import pytest
from lab_manager.nodes import NodeObservation
from lab_manager.reservation_models import NodeResourceLedger, NodeResourcePolicy

pytestmark = pytest.mark.integration


def policy(version=0, storage="student-lvm"):
    return {
        "storage_name": storage,
        "host_reserve_mib": 2048,
        "infrastructure_reserve_mib": 1024,
        "safety_reserve_mib": 1024,
        "cpu_millicredits_per_logical_cpu": 1000,
        "storage_free_percent": 10,
        "thin_metadata_limit_percent": 80,
        "expected_version": version,
    }


async def test_admin_sets_policy_with_version_and_teacher_cannot_change_it(
    app, seed, session, client_factory
):
    node_id = uuid.uuid4()
    now = datetime.now(UTC)
    async with app.state.sessions() as db, db.begin():
        db.add(
            NodeObservation(
                id=node_id,
                name="Synthetic node",
                attempt_started_at=now,
                last_contact_at=now,
                sample_finished_at=now,
                payload={
                    "sample": {
                        "storages": [
                            {"name": "student-lvm", "backend": "lvmthin", "active": True},
                            {"name": "local", "backend": "dir", "active": True},
                        ]
                    }
                },
            )
        )
    path = f"/api/admin/nodes/{node_id}/resource-policy"
    async with client_factory() as admin, client_factory() as teacher:
        await session(admin, await seed("ADMIN"))
        await session(teacher, await seed("TEACHER"))
        assert (await teacher.put(path, json=policy())).status_code == 403
        assert (await admin.put(path, json=policy(storage="local"))).status_code == 409
        first = await admin.put(path, json=policy())
        assert first.status_code == 200, first.text
        assert first.json()["version"] == 1
        assert (await admin.put(path, json=policy())).status_code == 409
        changed = await admin.put(path, json=policy(version=1))
        assert changed.status_code == 200, changed.text
        assert changed.json()["version"] == 2
        assert (await admin.get(path)).json()["storage_name"] == "student-lvm"
    async with app.state.sessions() as db:
        assert await db.get(NodeResourceLedger, node_id) is not None
        assert (await db.get(NodeResourcePolicy, node_id)).version == 2
