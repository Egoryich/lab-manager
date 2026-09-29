import asyncio
import uuid
from datetime import timedelta

import pytest
from lab_manager.operation_models import Operation, OperationEvent
from lab_manager.worker import claim_next, execute_claim, finish, tick
from sqlalchemy import func, select, update
from test_catalog import catalog_setup

pytestmark = pytest.mark.integration


async def setup_environment(client_factory, seed, session):
    owner = await seed("TEACHER")
    async with client_factory() as admin, client_factory() as teacher:
        await session(admin, await seed("ADMIN"))
        await session(teacher, owner)
        profiles, _ = await catalog_setup(admin, owner.id)
        group = (await teacher.post("/api/groups", json={"name": "Operations"})).json()
        response = await teacher.post(
            "/api/environments",
            json={
                "name": "Check configuration",
                "group_id": group["id"],
                "profile_version_id": profiles[0],
                "demo_profile_version_id": profiles[1],
                "request_id": str(uuid.uuid4()),
            },
        )
        assert response.status_code == 201, response.text
        return owner, response.json()


async def enqueue(teacher, environment, body=None):
    return await teacher.post(
        f"/api/environments/{environment['id']}/validate",
        json=body
        or {
            "expected_version": environment["version"],
            "request_id": str(uuid.uuid4()),
        },
    )


async def test_operation_races_replay_privacy_and_execution(app, client_factory, seed, session):
    owner, environment = await setup_environment(client_factory, seed, session)
    async with client_factory() as teacher, client_factory() as other, client_factory() as student:
        await session(teacher, owner)
        await session(other, await seed("TEACHER"))
        await session(student, await seed())
        body = {"expected_version": environment["version"], "request_id": str(uuid.uuid4())}
        responses = await asyncio.gather(*(enqueue(teacher, environment, body) for _ in range(8)))
        assert all(r.status_code == 202 for r in responses)
        operation = responses[0].json()
        assert len({r.json()["id"] for r in responses}) == 1
        assert (await enqueue(teacher, environment)).json()["code"] == "OPERATION_IN_PROGRESS"
        assert (await enqueue(teacher, environment, body | {"expected_version": 2})).json()[
            "code"
        ] == "IDEMPOTENCY_CONFLICT"
        path = f"/api/operations/{operation['id']}"
        assert (await other.get(path)).status_code == 404
        assert (await other.get("/api/operations")).json() == []
        assert (await enqueue(other, environment)).status_code == 404
        assert (await student.get(path)).status_code == 403
        assert (await enqueue(student, environment)).status_code == 403
        claims = await asyncio.gather(
            *(claim_next(app.state.sessions, uuid.uuid4()) for _ in range(6))
        )
        assert sum(c is not None for c in claims) == 1
        await execute_claim(app.state.sessions, next(c for c in claims if c))
        completed = (await teacher.get(path)).json()
        assert completed["state"] == "SUCCEEDED"
        assert completed["result"]["scope"] == "CONFIGURATION_ONLY"
        assert completed["result"]["estimate"]["reservation_created"] is False
        assert completed["result"]["estimate"]["demo"]["machines"] == 1
        assert "lease_owner" not in completed and "request_digest" not in completed
        assert (await enqueue(teacher, environment, body)).json()["id"] == operation["id"]
        async with app.state.sessions() as db:
            events = list(await db.scalars(select(OperationEvent).order_by(OperationEvent.version)))
            assert [(e.version, e.state) for e in events] == [
                (1, "QUEUED"),
                (2, "RUNNING"),
                (3, "SUCCEEDED"),
            ]


async def test_expired_lease_is_reclaimed_and_old_worker_cannot_finish(
    app,
    client_factory,
    seed,
    session,
):
    owner, environment = await setup_environment(client_factory, seed, session)
    async with client_factory() as teacher:
        await session(teacher, owner)
        operation = (await enqueue(teacher, environment)).json()
        first = await claim_next(app.state.sessions, uuid.uuid4())
        # Represents a stopped process with a committed claim, not a DB rollback.
        async with app.state.sessions() as db, db.begin():
            await db.execute(
                update(Operation)
                .values(lease_until=func.clock_timestamp() - timedelta(seconds=1))
                .where(Operation.id == first.id)
            )
        async with app.state.sessions() as db, db.begin():
            assert not await finish(db, first, error_code="STALE_WORKER")
        second = await claim_next(app.state.sessions, uuid.uuid4())
        assert second.id == first.id and second.fence > first.fence
        async with app.state.sessions() as db, db.begin():
            assert not await finish(db, first, error_code="STALE_WORKER")
        await execute_claim(app.state.sessions, second)
        assert (await teacher.get(f"/api/operations/{operation['id']}")).json()[
            "state"
        ] == "SUCCEEDED"


async def test_worker_rechecks_revoked_policy_and_does_not_touch_unknown_jobs(
    app,
    client_factory,
    seed,
    session,
):
    from lab_manager.catalog_models import TeacherPolicyAssignment

    owner, environment = await setup_environment(client_factory, seed, session)
    async with client_factory() as teacher, client_factory() as admin:
        await session(teacher, owner)
        await session(admin, await seed("ADMIN"))
        assert (await admin.get("/api/admin/worker-status")).json()["state"] == "UNAVAILABLE"
        operation = (await enqueue(teacher, environment)).json()
        async with app.state.sessions() as db, db.begin():
            assignment = await db.get(TeacherPolicyAssignment, owner.id)
            await db.delete(assignment)
        await tick(app.state.sessions, uuid.uuid4())
        result = (await teacher.get(f"/api/operations/{operation['id']}")).json()
        assert result["state"] == "FAILED" and result["error_code"] == "PROFILE_FORBIDDEN"
        assert (await admin.get("/api/admin/worker-status")).json()["state"] == "AVAILABLE"
        assert (await teacher.get("/api/admin/worker-status")).status_code == 403
        next_op = (await enqueue(teacher, environment)).json()
        async with app.state.sessions() as db, db.begin():
            await db.execute(
                update(Operation)
                .where(Operation.id == uuid.UUID(next_op["id"]))
                .values(kind="FUTURE_PROVIDER_COMMAND")
            )
        assert await claim_next(app.state.sessions, uuid.uuid4()) is None


async def test_worker_failure_retries_without_secrets_and_has_attempt_limit(
    app,
    client_factory,
    seed,
    session,
    monkeypatch,
):
    from lab_manager import worker

    owner, environment = await setup_environment(client_factory, seed, session)

    async def broken(*args):
        raise RuntimeError("provider password=secret-must-not-leak")

    monkeypatch.setattr(worker, "validate_environment", broken)
    async with client_factory() as teacher:
        await session(teacher, owner)
        operation = (await enqueue(teacher, environment)).json()
        for attempt in range(worker.MAX_ATTEMPTS):
            claim = await claim_next(app.state.sessions, uuid.uuid4())
            assert claim is not None
            await execute_claim(app.state.sessions, claim)
            response = await teacher.get(f"/api/operations/{operation['id']}")
            assert "secret-must-not-leak" not in response.text
            assert response.json()["state"] == (
                "FAILED" if attempt == worker.MAX_ATTEMPTS - 1 else "QUEUED"
            )
            async with app.state.sessions() as db, db.begin():
                await db.execute(
                    update(Operation)
                    .where(Operation.id == claim.id)
                    .values(available_at=func.clock_timestamp())
                )
        assert await claim_next(app.state.sessions, uuid.uuid4()) is None


async def test_job_and_outbox_rollback_together(app, client_factory, seed, session, monkeypatch):
    from lab_manager import operations

    owner, environment = await setup_environment(client_factory, seed, session)

    def audit_failure(*args, **kwargs):
        raise RuntimeError("producer aborted before commit")

    monkeypatch.setattr(operations, "audit", audit_failure)
    async with client_factory() as teacher:
        await session(teacher, owner)
        with pytest.raises(RuntimeError, match="producer aborted"):
            await enqueue(teacher, environment)
    async with app.state.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Operation)) == 0
        assert await db.scalar(select(func.count()).select_from(OperationEvent)) == 0
