import asyncio

import pytest
from lab_manager.models import AuditEvent, GroupJoinCode, GroupMember, LocalCredential
from sqlalchemy import func, select

pytestmark = pytest.mark.integration
PASSWORD = "test-password-with-entropy"


async def create_group(client):
    result = await client.post("/api/groups", json={"name": "Системное администрирование"})
    assert result.status_code == 201, result.text
    return result.json()


async def test_registration_login_csrf_logout(app, client_factory):
    async with client_factory() as client:
        payload = {"username": "Student.One", "display_name": "Иван Петров", "password": PASSWORD}
        elevated = await client.post("/api/auth/register", json=payload | {"roles": ["ADMIN"]})
        assert elevated.status_code == 422 and PASSWORD not in elevated.text
        registered = await client.post("/api/auth/register", json=payload)
        assert registered.status_code == 201
        assert registered.json()["roles"] == ["STUDENT"]
        assert registered.json()["username"] == "student.one"
        assert (await client.post("/api/auth/register", json=payload)).status_code == 409
        login = await client.post(
            "/api/auth/login", json={"username": payload["username"], "password": PASSWORD}
        )
        assert login.status_code == 200, login.text
        assert "HttpOnly" in login.headers["set-cookie"]
        assert (await client.get("/api/auth/me")).status_code == 200
        assert (await client.post("/api/auth/logout")).status_code == 403
        client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        assert (
            await client.post("/api/auth/logout", headers={"Origin": "https://evil.test"})
        ).status_code == 403
        cookie = client.cookies.get("lab_session")
        assert (await client.post("/api/auth/logout")).status_code == 204
        client.cookies.set("lab_session", cookie)
        assert (await client.get("/api/auth/me")).status_code == 401
    async with app.state.sessions() as db:
        stored = await db.scalar(select(LocalCredential.password_hash))
        assert stored.startswith("$argon2id$") and PASSWORD not in stored


async def test_teacher_isolation_student_projection_and_join_idempotency(
    app,
    client_factory,
    seed,
    session,
):
    owner, other, student = await seed("TEACHER"), await seed("TEACHER"), await seed()
    async with (
        client_factory() as teacher,
        client_factory() as stranger,
        client_factory() as learner,
    ):
        await session(teacher, owner)
        await session(stranger, other)
        await session(learner, student)
        group = await create_group(teacher)
        path = "/api/groups/" + group["id"]
        assert (await stranger.get("/api/groups")).json() == []
        assert (await stranger.get(path)).status_code == 404
        assert (await stranger.get(path + "/members")).status_code == 404
        assert (
            await stranger.patch(path, json={"expected_version": 1, "join_enabled": False})
        ).status_code == 404
        assert (await learner.get(path)).status_code == 404
        for _ in range(2):
            joined = await learner.post(
                "/api/groups/join", json={"code": group["join_code"].lower(), "confirm": True}
            )
            assert joined.status_code == 200, joined.text
            assert joined.json()["join_code"] is None and joined.json()["member_count"] is None
        assert (await learner.get(path + "/members")).status_code == 403
        assert (await learner.get(path)).status_code == 200
        assert len((await teacher.get(path + "/members")).json()) == 1
        assert (await learner.get("/api/admin/users")).status_code == 403
    async with app.state.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(GroupMember)) == 1
        stored = await db.scalar(select(GroupJoinCode))
        assert group["join_code"] not in stored.ciphertext
        assert group["join_code"] != stored.digest
        events = list(await db.scalars(select(AuditEvent)))
        assert group["join_code"] not in str([e.details for e in events])
        assert len([e for e in events if e.action == "group.joined"]) == 1


async def test_rotation_disable_version_and_existing_members(client_factory, seed, session):
    async with client_factory() as teacher, client_factory() as student:
        await session(teacher, await seed("TEACHER"))
        await session(student, await seed())
        group = await create_group(teacher)
        path = "/api/groups/" + group["id"]
        rotated = await teacher.post(path + "/join-code/regenerate", json={"expected_version": 1})
        assert rotated.status_code == 200
        assert (
            await student.post(
                "/api/groups/join", json={"code": group["join_code"], "confirm": True}
            )
        ).status_code == 404
        assert (
            await student.post(
                "/api/groups/join", json={"code": rotated.json()["join_code"], "confirm": True}
            )
        ).status_code == 200
        stale = await teacher.patch(path, json={"expected_version": 1, "join_enabled": False})
        assert stale.status_code == 409
        current = (await teacher.get(path)).json()
        assert (
            await teacher.patch(
                path, json={"expected_version": current["version"], "join_enabled": False}
            )
        ).status_code == 200
        assert (await student.get(path)).status_code == 200
        assert (
            await student.post(
                "/api/groups/join", json={"code": rotated.json()["join_code"], "confirm": True}
            )
        ).status_code == 404


async def test_thirty_concurrent_students_and_duplicate_join(app, client_factory, seed, session):
    async with client_factory() as teacher:
        await session(teacher, await seed("TEACHER"))
        group = await create_group(teacher)
    students = [await seed() for _ in range(30)]

    async def join(student):
        async with client_factory() as client:
            await session(client, student)
            responses = await asyncio.gather(
                *[
                    client.post(
                        "/api/groups/join", json={"code": group["join_code"], "confirm": True}
                    )
                    for _ in range(2)
                ]
            )
            assert all(r.status_code == 200 for r in responses), [r.text for r in responses]

    await asyncio.gather(*(join(student) for student in students))
    async with app.state.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(GroupMember)) == 30
        assert (
            await db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "group.joined")
            )
            == 30
        )


async def test_reset_revoke_single_use_and_admin_permission(client_factory, seed, session):
    admin, user = await seed("ADMIN"), await seed()
    async with client_factory() as administrator, client_factory() as student:
        await session(administrator, admin)
        await session(student, user)
        path = f"/api/admin/users/{user.id}/password-reset"
        assert (await student.post(path)).status_code == 403
        issued = await administrator.post(path)
        assert issued.status_code == 200
        assert (await student.get("/api/auth/me")).status_code == 401
        body = {"token": issued.json()["token"], "password": "new-secure-password"}
        results = await asyncio.gather(
            *(student.post("/api/auth/reset-password", json=body) for _ in range(2))
        )
        assert sorted(r.status_code for r in results) == [204, 400]
        assert (
            await student.post(
                "/api/auth/login", json={"username": user.username, "password": PASSWORD}
            )
        ).status_code == 401
        assert (
            await student.post(
                "/api/auth/login", json={"username": user.username, "password": body["password"]}
            )
        ).status_code == 200


async def test_teacher_grant_and_revocation(client_factory, seed, session):
    admin, user = await seed("ADMIN"), await seed()
    async with client_factory() as administrator, client_factory() as teacher:
        await session(administrator, admin)
        await session(teacher, user)
        assert (await teacher.post("/api/groups", json={"name": "Test"})).status_code == 403
        path = f"/api/admin/users/{user.id}/teacher"
        assert (await administrator.put(path, json={"can_create_groups": True})).status_code == 200
        assert (await teacher.get("/api/auth/me")).status_code == 401
        logged = await teacher.post(
            "/api/auth/login", json={"username": user.username, "password": PASSWORD}
        )
        teacher.headers["X-CSRF-Token"] = logged.json()["csrf_token"]
        await create_group(teacher)
        assert (await administrator.put(path, json={"can_create_groups": False})).status_code == 200
        logged = await teacher.post(
            "/api/auth/login", json={"username": user.username, "password": PASSWORD}
        )
        teacher.headers["X-CSRF-Token"] = logged.json()["csrf_token"]
        assert (await teacher.post("/api/groups", json={"name": "Test"})).status_code == 403
        assert len((await teacher.get("/api/groups")).json()) == 1


async def test_join_rate_limit_and_readiness(client_factory, seed, session):
    async with client_factory() as client:
        await session(client, await seed())
        for _ in range(10):
            assert (
                await client.post("/api/groups/join", json={"code": "ABC234", "confirm": True})
            ).status_code == 404
        assert (
            await client.post("/api/groups/join", json={"code": "ABC234", "confirm": True})
        ).status_code == 429
        assert (await client.get("/api/health/ready")).status_code == 200


async def test_code_collision_retries_without_losing_group(
    client_factory, seed, session, monkeypatch
):
    from lab_manager import groups

    codes = iter(["ABC234", "ABC234", "DEF567"])
    monkeypatch.setattr(groups, "new_join_code", lambda: next(codes))
    async with client_factory() as client:
        await session(client, await seed("TEACHER"))
        first = await create_group(client)
        second = await create_group(client)
        assert first["join_code"] == "ABC234"
        assert second["join_code"] == "DEF567"
        assert len((await client.get("/api/groups")).json()) == 2


async def test_rate_limiter_failure_denies_mutation(
    app, client_factory, seed, session, monkeypatch
):
    from redis.exceptions import ConnectionError

    async def unavailable(*args, **kwargs):
        raise ConnectionError("test failure")

    monkeypatch.setattr(app.state.redis, "eval", unavailable)
    async with client_factory() as client:
        await session(client, await seed("TEACHER"))
        result = await client.post("/api/groups", json={"name": "Not created"})
        assert result.status_code == 503
        assert result.json()["code"] == "RATE_LIMIT_UNAVAILABLE"
        assert (await client.get("/api/groups")).json() == []
