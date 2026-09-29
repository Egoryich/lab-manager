"""Durable worker. Only the explicitly replay-safe configuration check is enabled.

External effects need provider receipts/reconciliation before they can be registered;
an expired lease alone is never permission to repeat a Proxmox or Tuya command.
"""

import argparse
import asyncio
import logging
import signal
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from sqlalchemy import delete, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from lab_manager.catalog import configuration, estimate
from lab_manager.catalog_models import Environment
from lab_manager.catalog_schemas import EnvironmentCreate
from lab_manager.config import Settings
from lab_manager.dependencies import Problem
from lab_manager.models import User, UserRole
from lab_manager.node_transport import load_endpoints
from lab_manager.nodes import poll_forever
from lab_manager.operation_models import Operation, WorkerHeartbeat
from lab_manager.operations import VALIDATE, ValidationResult, event

logger = logging.getLogger("lab_manager.worker")
LEASE_SECONDS = 30
MAX_ATTEMPTS = 5
HEALTH_FILE = Path(tempfile.gettempdir()) / "lab-manager-worker-health"


@dataclass(frozen=True)
class Claim:
    id: uuid.UUID
    worker_id: uuid.UUID
    fence: int
    actor_id: uuid.UUID
    environment_id: uuid.UUID
    expected_version: int


@dataclass(frozen=True)
class WorkerActor:
    id: uuid.UUID
    roles: list[str]


async def transaction_limits(db):
    await db.execute(text("SET LOCAL statement_timeout = '10s'"))
    await db.execute(text("SET LOCAL lock_timeout = '3s'"))


async def heartbeat(sessions, worker_id):
    async with sessions() as db, db.begin():
        await transaction_limits(db)
        now = await db.scalar(select(func.clock_timestamp()))
        await db.execute(
            insert(WorkerHeartbeat)
            .values(id=worker_id, seen_at=now)
            .on_conflict_do_update(index_elements=[WorkerHeartbeat.id], set_={"seen_at": now})
        )
        await db.execute(
            delete(WorkerHeartbeat).where(WorkerHeartbeat.seen_at < now - timedelta(days=1))
        )


async def claim_next(sessions, worker_id) -> Claim | None:
    async with sessions() as db, db.begin():
        await transaction_limits(db)
        operation = await db.scalar(
            select(Operation)
            .where(
                Operation.kind == VALIDATE,
                or_(
                    (Operation.state == "QUEUED")
                    & (Operation.available_at <= func.clock_timestamp()),
                    # This handler has no external effects. Do NOT generalize this condition
                    # to future create/start/stop/delete/power operations.
                    (Operation.state == "RUNNING")
                    & (Operation.lease_until <= func.clock_timestamp()),
                ),
            )
            .order_by(Operation.available_at, Operation.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if operation is None:
            return None
        now = await db.scalar(select(func.clock_timestamp()))
        operation.version += 1
        if operation.attempt >= MAX_ATTEMPTS:
            operation.state = "FAILED"
            operation.error_code = "WORKER_RETRY_EXHAUSTED"
            operation.finished_at = now
            operation.lease_owner = operation.lease_until = None
            event(db, operation)
            return None
        operation.state = "RUNNING"
        operation.attempt += 1
        operation.fence += 1
        operation.lease_owner = worker_id
        operation.lease_until = now + timedelta(seconds=LEASE_SECONDS)
        operation.error_code = None
        event(db, operation)
        return Claim(
            operation.id,
            worker_id,
            operation.fence,
            operation.actor_id,
            operation.environment_id,
            operation.expected_version,
        )


async def leased_operation(db, claim):
    operation = await db.scalar(
        select(Operation)
        .where(
            Operation.id == claim.id,
        )
        .with_for_update()
    )
    # Check database time after acquiring the lock: waiting must not extend a lease.
    now = await db.scalar(select(func.clock_timestamp()))
    if (
        operation is None
        or operation.state != "RUNNING"
        or operation.lease_owner != claim.worker_id
        or operation.fence != claim.fence
        or operation.lease_until <= now
    ):
        return None, now
    return operation, now


async def finish(db, claim, *, result=None, error_code=None, retry=False):
    operation, now = await leased_operation(db, claim)
    if operation is None:
        return False
    operation.version += 1
    operation.lease_owner = operation.lease_until = None
    operation.result = result
    operation.error_code = error_code
    if retry and operation.attempt < MAX_ATTEMPTS:
        operation.state = "QUEUED"
        operation.available_at = now + timedelta(seconds=min(60, 2**operation.attempt))
    else:
        operation.state = "FAILED" if error_code else "SUCCEEDED"
        operation.finished_at = now
    event(db, operation)
    return True


async def validate_environment(db, claim):
    # Same lock order as API policy/roster changes; a queued job is not authority.
    user = await db.scalar(select(User).where(User.id == claim.actor_id).with_for_update())
    roles = list(await db.scalars(select(UserRole.role).where(UserRole.user_id == claim.actor_id)))
    if not user or user.status != "ACTIVE" or "TEACHER" not in roles:
        raise Problem(403, "FORBIDDEN", "Недостаточно прав.")
    environment = await db.get(Environment, claim.environment_id)
    if not environment or environment.owner_teacher_id != claim.actor_id:
        raise Problem(404, "NOT_FOUND", "Окружение не найдено.")
    if environment.version != claim.expected_version:
        raise Problem(409, "VERSION_CONFLICT", "Окружение изменилось.")
    body = EnvironmentCreate(
        name=environment.name,
        group_id=environment.group_id,
        profile_version_id=environment.profile_version_id,
        demo_profile_version_id=environment.demo_profile_version_id,
        request_id=environment.request_id,
    )
    group, policy, selected = await configuration(
        db, WorkerActor(claim.actor_id, roles), body, lock=True
    )
    # Environment updates must follow User -> Group -> Environment too.
    await db.refresh(environment, with_for_update=True)
    if (
        environment.version != claim.expected_version
        or environment.owner_teacher_id != claim.actor_id
    ):
        raise Problem(409, "VERSION_CONFLICT", "Окружение изменилось.")
    result = await estimate(db, group, *selected, policy)
    return ValidationResult(
        checked_at=await db.scalar(select(func.clock_timestamp())),
        environment_version=environment.version,
        permission_revision_id=policy.revision_id,
        assignment_version=policy.assignment_version,
        estimate=result,
    ).model_dump(mode="json")


async def execute_claim(sessions, claim):
    try:
        async with asyncio.timeout(20), sessions() as db, db.begin():
            await transaction_limits(db)
            try:
                result = await validate_environment(db, claim)
            except Problem as error:
                await finish(db, claim, error_code=error.code)
            else:
                await finish(db, claim, result=result)
    except Exception as error:
        # Never persist/log exception text: DB/provider errors can contain secrets.
        logger.warning("operation_failure operation=%s type=%s", claim.id, type(error).__name__)
        async with sessions() as db, db.begin():
            await transaction_limits(db)
            await finish(db, claim, error_code="WORKER_RETRY_REQUIRED", retry=True)


async def tick(sessions, worker_id):
    await heartbeat(sessions, worker_id)
    claim = await claim_next(sessions, worker_id)
    if claim:
        await execute_claim(sessions, claim)
    return claim is not None


async def run(once=False):
    settings = Settings()
    engine = create_async_engine(
        settings.database_url.get_secret_value(),
        pool_size=1,
        max_overflow=0,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={"connect_timeout": 5},
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop.set)
        except NotImplementedError:
            pass  # Windows development; Linux containers use graceful signals.
    worker_id = uuid.uuid4()
    poller = None
    try:
        async with engine.connect() as connection:
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            if revision != "0004_nodes":
                raise RuntimeError("Worker requires migration 0004_nodes")
        endpoints = load_endpoints(settings.node_config or None)
        if endpoints and not once:
            poller = asyncio.create_task(poll_forever(sessions, endpoints, stop))
        while not stop.is_set():
            delay = 2
            try:
                if await tick(sessions, worker_id):
                    delay = 0.1
                HEALTH_FILE.write_text(str(time.monotonic()))
            except Exception as error:
                if once:
                    raise
                logger.warning("worker_unavailable type=%s", type(error).__name__)
                delay = 5
            if once:
                break
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                pass
    finally:
        stop.set()
        if poller:
            await poller
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Process at most one ready job")
    parser.add_argument("--healthcheck", action="store_true")
    args = parser.parse_args()
    if args.healthcheck:
        try:
            age = time.monotonic() - float(HEALTH_FILE.read_text())
        except (OSError, ValueError):
            raise SystemExit(1) from None
        raise SystemExit(0 if 0 <= age <= 30 else 1)
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run(once=args.once))


if __name__ == "__main__":
    main()
