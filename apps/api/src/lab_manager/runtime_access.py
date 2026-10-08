"""Grant a browser-only Guacamole terminal to an active owned runtime."""

import secrets
import uuid
from datetime import timedelta

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel
from sqlalchemy import func, select, update

from lab_manager.access_models import AccessGrant
from lab_manager.audit import audit
from lab_manager.catalog_models import Environment
from lab_manager.dependencies import Actor, Problem
from lab_manager.models import Group, GroupMember
from lab_manager.network_models import NetworkSegmentAllocation
from lab_manager.reservation_models import LessonReservation
from lab_manager.runtime_models import (
    EnvironmentRun,
    RunRuntime,
    Runtime,
    RuntimeSshCredential,
)

router = APIRouter(tags=["Lessons"])


class BrowserTerminal(BaseModel):
    url: str


def may_open(actor, runtime, environment, membership) -> bool:
    if "TEACHER" in actor.roles and environment.owner_teacher_id == actor.id:
        return True
    return (
        "STUDENT" in actor.roles
        and runtime.role == "STUDENT"
        and runtime.student_id == actor.id
        and membership is not None
        and membership.status == "ACTIVE"
        and membership.generation == runtime.membership_generation
    )


def launch_response(response: Response, environment: str, nonce: str) -> BrowserTerminal:
    """Keep the one-time nonce out of URLs, JSON bodies and browser JavaScript."""
    cookie_name = "__Secure-lab_guac_launch" if environment == "production" else "lab_guac_launch"
    response.set_cookie(
        key=cookie_name,
        value=nonce,
        max_age=45,
        path="/guacamole",
        secure=environment == "production",
        httponly=True,
        samesite="strict",
    )
    response.headers["Cache-Control"] = "no-store"
    return BrowserTerminal(url="/guacamole/")


@router.post("/runtimes/{runtime_id}/open", response_model=BrowserTerminal)
async def open_runtime(runtime_id: uuid.UUID, actor: Actor, request: Request, response: Response):
    settings = request.app.state.settings
    if not settings.guacamole_broker_enabled:
        raise Problem(503, "GUACAMOLE_NOT_CONFIGURED", "Браузерный терминал пока недоступен.")
    async with request.app.state.sessions() as db, db.begin():
        runtime = await db.get(Runtime, runtime_id)
        if runtime is None:
            raise Problem(404, "NOT_FOUND", "Машина не найдена.")
        environment = await db.get(Environment, runtime.environment_id)
        group = await db.get(Group, environment.group_id) if environment else None
        if environment is None or group is None or group.archived_at is not None:
            raise Problem(404, "NOT_FOUND", "Машина не найдена.")
        membership = None
        if "STUDENT" in actor.roles and runtime.student_id == actor.id:
            membership = await db.scalar(
                select(GroupMember).where(
                    GroupMember.group_id == group.id, GroupMember.student_id == actor.id
                )
            )
        if not may_open(actor, runtime, environment, membership):
            raise Problem(404, "NOT_FOUND", "Машина не найдена.")
        run = await db.scalar(
            select(EnvironmentRun)
            .join(RunRuntime, RunRuntime.run_id == EnvironmentRun.id)
            .where(
                RunRuntime.runtime_id == runtime_id,
                RunRuntime.state == "ACTIVE",
                EnvironmentRun.environment_id == environment.id,
                EnvironmentRun.state == "RUNNING",
            )
        )
        now = await db.scalar(select(func.clock_timestamp()))
        reservation = await db.get(LessonReservation, run.reservation_id) if run else None
        allocation = (
            await db.get(NetworkSegmentAllocation, runtime.network_allocation_id)
            if runtime.network_allocation_id
            else None
        )
        credential = await db.get(RuntimeSshCredential, runtime_id)
        if (
            run is None
            or reservation is None
            or reservation.state != "ACTIVE"
            or reservation.ends_at <= now
            or reservation.environment_id != environment.id
            or runtime.kind != "LXC"
            or runtime.state != "RUNNING"
            or allocation is None
            or allocation.state != "APPLIED"
            or credential is None
            or not credential.host_key
            or not runtime.guest_ipv4
        ):
            raise Problem(409, "RUNTIME_NOT_AVAILABLE", "Машина пока недоступна.")
        await db.execute(
            update(AccessGrant)
            .where(
                AccessGrant.browser_session_id == actor.session.id,
                AccessGrant.runtime_id == runtime.id,
                AccessGrant.consumed_at.is_(None),
                AccessGrant.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        nonce = secrets.token_urlsafe(32)
        db.add(
            AccessGrant(
                nonce_digest=request.app.state.codec.digest("guacamole_launch", nonce),
                user_id=actor.id,
                browser_session_id=actor.session.id,
                runtime_id=runtime.id,
                environment_run_id=run.id,
                expires_at=now + timedelta(seconds=45),
            )
        )
        audit(
            db,
            request,
            actor.id,
            "runtime.browser_launch_requested",
            runtime.id,
            environment_id=str(environment.id),
        )
    return launch_response(response, settings.environment, nonce)
