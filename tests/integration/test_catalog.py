import asyncio
import uuid

import pytest
from lab_manager.catalog_models import Environment
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

pytestmark = pytest.mark.integration


async def catalog_setup(admin, teacher_id, *, ram_limit=65536):
    profiles = []
    for kind, ram in [("LXC", 512), ("QEMU", 2048)]:
        template = await admin.post(
            "/api/admin/template-versions",
            json={
                "name": "Linux",
                "version_label": "test-1",
                "runtime_kind": kind,
                "guest_family": "LINUX",
            },
        )
        assert template.status_code == 201, template.text
        profile = await admin.post(
            "/api/admin/profile-versions",
            json={
                "name": kind,
                "template_version_id": template.json()["id"],
                "memory_mib": ram,
                "vcpu": 1,
                "cpu_millicredits": 500,
                "disk_gib": 10,
                "network_mode": "ISOLATED",
                "internet_enabled": False,
            },
        )
        assert profile.status_code == 201, profile.text
        profiles.append(profile.json()["id"])
    policy = await admin.post(
        "/api/admin/permission-policies",
        json={
            "name": "LXC with QEMU demo",
            "permissions": {"can_create_lxc": True, "can_use_linux_profiles": True},
            "limits": {
                "max_lxc_per_environment": 30,
                "max_vm_per_environment": 0,
                "max_total_ram_mb": ram_limit,
                "max_cpu_credits": 32,
                "max_disk_gb": 400,
                "max_active_environments": 1,
            },
            "demo_profile_ids": [profiles[1]],
        },
    )
    assert policy.status_code == 201, policy.text
    assignment = await admin.put(
        f"/api/admin/teachers/{teacher_id}/policy",
        json={"revision_id": policy.json()["id"], "expected_version": 0},
    )
    assert assignment.status_code == 200, assignment.text
    return profiles, policy.json()


async def test_catalog_demo_estimate_idempotency_and_privacy(app, client_factory, seed, session):
    owner = await seed("TEACHER")
    async with client_factory() as admin, client_factory() as teacher, client_factory() as other:
        await session(admin, await seed("ADMIN"))
        await session(teacher, owner)
        await session(other, await seed("TEACHER"))
        profiles, policy = await catalog_setup(admin, owner.id)
        visible = (await teacher.get("/api/profiles")).json()
        qemu = next(p for p in visible if p["runtime_kind"] == "QEMU")
        assert qemu["demo_allowed"] and not qemu["student_allowed"]
        assert (await other.get("/api/profiles")).json() == []
        group = (await teacher.post("/api/groups", json={"name": "Linux"})).json()
        # Use real group joins so the estimate reads the committed membership.
        for _ in range(2):
            async with client_factory() as learner:
                await session(learner, await seed())
                assert (
                    await learner.post(
                        "/api/groups/join", json={"code": group["join_code"], "confirm": True}
                    )
                ).status_code == 200
        body = {
            "name": "Lesson",
            "group_id": group["id"],
            "profile_version_id": profiles[0],
            "demo_profile_version_id": profiles[1],
            "request_id": str(uuid.uuid4()),
        }
        estimate = await teacher.post("/api/environments/estimate", json=body)
        assert estimate.status_code == 200, estimate.text
        result = estimate.json()
        assert result["total"]["memory_mib"] == 3072
        assert result["total"]["disk_bytes"] == 30 * 2**30
        assert result["total"]["hibernation_bytes"] == 2 * 2**30
        assert not result["reservation_created"] and result["admission_status"] == "NOT_CHECKED"
        assert (await other.post("/api/environments", json=body)).status_code == 404
        forbidden = await teacher.post(
            "/api/environments", json=body | {"profile_version_id": profiles[1]}
        )
        assert forbidden.status_code == 403
        missing_demo = body.copy()
        del missing_demo["demo_profile_version_id"]
        assert (await teacher.post("/api/environments", json=missing_demo)).status_code == 422
        responses = await asyncio.gather(
            *(teacher.post("/api/environments", json=body) for _ in range(2))
        )
        assert all(r.status_code == 201 for r in responses), [r.text for r in responses]
        assert responses[0].json()["id"] == responses[1].json()["id"]
        assert (
            await teacher.post("/api/environments", json=body | {"name": "Changed"})
        ).status_code == 409
        assert (await other.get("/api/environments")).json() == []
        assert (await other.get(f"/api/teachers/{owner.id}/permissions")).status_code == 404
    async with app.state.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Environment)) == 1
        # Immutable profile protects already pinned environments from in-place edits.
        with pytest.raises(DBAPIError):
            await db.execute(text("UPDATE profile_versions SET memory_mib = 4096"))
        await db.rollback()


async def test_quota_network_policy_and_assignment_version(client_factory, seed, session):
    owner = await seed("TEACHER")
    async with client_factory() as admin, client_factory() as teacher:
        await session(admin, await seed("ADMIN"))
        await session(teacher, owner)
        profiles, policy = await catalog_setup(admin, owner.id, ram_limit=1024)
        group = (await teacher.post("/api/groups", json={"name": "Group"})).json()
        body = {
            "name": "Too much RAM",
            "group_id": group["id"],
            "profile_version_id": profiles[0],
            "demo_profile_version_id": profiles[1],
            "request_id": str(uuid.uuid4()),
        }
        result = await teacher.post("/api/environments/estimate", json=body)
        assert "max_total_ram_mb" in result.json()["violations"]
        assert (await teacher.post("/api/environments", json=body)).status_code == 409
        assert (
            await admin.put(
                f"/api/admin/teachers/{owner.id}/policy",
                json={"revision_id": policy["id"], "expected_version": 0},
            )
        ).status_code == 409
        policy_body = {k: v for k, v in policy.items() if k != "id"}
        policy_body["permissions"]["can_create_snapshots"] = True
        assert (
            await admin.post("/api/admin/permission-policies", json=policy_body)
        ).status_code == 422
        assert (
            await teacher.post(
                "/api/admin/permission-policies", json=policy_body | {"permissions": {}}
            )
        ).status_code == 403
        original = next(
            p for p in (await admin.get("/api/profiles")).json() if p["id"] == profiles[0]
        )
        profile_body = {
            k: original[k]
            for k in [
                "name",
                "template_version_id",
                "memory_mib",
                "vcpu",
                "cpu_millicredits",
                "disk_gib",
                "network_mode",
                "internet_enabled",
            ]
        }
        internet = (
            await admin.post(
                "/api/admin/profile-versions", json=profile_body | {"internet_enabled": True}
            )
        ).json()
        assert (
            await teacher.post(
                "/api/environments/estimate", json=body | {"profile_version_id": internet["id"]}
            )
        ).status_code == 403
        shared = (
            await admin.post(
                "/api/admin/profile-versions", json=profile_body | {"network_mode": "GROUP_LAN"}
            )
        ).json()
        assert (
            await teacher.post(
                "/api/environments/estimate", json=body | {"profile_version_id": shared["id"]}
            )
        ).status_code == 403
