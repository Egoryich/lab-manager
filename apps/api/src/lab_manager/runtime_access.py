"""Grant a browser-only Guacamole terminal to an active owned runtime."""

import uuid
from urllib.parse import quote

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel
from sqlalchemy import func, select

from lab_manager.audit import audit
from lab_manager.catalog_models import Environment
from lab_manager.dependencies import Actor, Problem
from lab_manager.guacamole_auth import SSHConnection, issue_ssh_grant
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


@router.post("/runtimes/{runtime_id}/open", response_model=BrowserTerminal)
async def open_runtime(runtime_id: uuid.UUID, actor: Actor, request: Request, response: Response):
    secret = request.app.state.settings.guacamole_json_secret
    if secret is None:
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
        ):
            raise Problem(409, "RUNTIME_NOT_AVAILABLE", "Машина пока недоступна.")
        token = issue_ssh_grant(
            secret.get_secret_value(),
            actor_id=actor.id,
            connection=SSHConnection(
                runtime_id=runtime.id,
                address=runtime.guest_ipv4,
                subnet=allocation.cidr,
                username="root",
                private_key=request.app.state.codec.decrypt(credential.private_key_ciphertext),
                host_key=credential.host_key,
            ),
            now=now,
        )
        audit(
            db,
            request,
            actor.id,
            "runtime.browser_opened",
            runtime.id,
            environment_id=str(environment.id),
        )
    response.headers["Cache-Control"] = "no-store"
    return BrowserTerminal(url="/guacamole/?data=" + quote(token, safe=""))
