"""Operation API. Producers commit the job, audit and outbox in one transaction."""

import hashlib
import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select

from lab_manager.audit import audit
from lab_manager.catalog_models import Environment
from lab_manager.catalog_schemas import EstimateView
from lab_manager.dependencies import DB, Actor, Problem, require_role
from lab_manager.models import User, UserRole
from lab_manager.operation_models import TERMINAL, Operation, OperationEvent, WorkerHeartbeat
from lab_manager.schemas import Input

router = APIRouter(tags=["Operations"])
VALIDATE = "ENVIRONMENT_VALIDATE"


class ValidationRequest(Input):
    request_id: uuid.UUID
    expected_version: int = Field(ge=1)


class ValidationResult(BaseModel):
    scope: Literal["CONFIGURATION_ONLY"] = "CONFIGURATION_ONLY"
    checked_at: datetime
    environment_version: int
    permission_revision_id: uuid.UUID
    assignment_version: int
    estimate: EstimateView


class OperationView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    environment_id: uuid.UUID
    kind: str
    state: Literal[
        "QUEUED",
        "RUNNING",
        "WAITING_NODE",
        "WAITING_RECONCILIATION",
        "SUCCEEDED",
        "FAILED",
        "CANCEL_REQUESTED",
        "CANCELLED",
    ]
    version: int
    created_at: datetime
    finished_at: datetime | None
    error_code: str | None
    result: ValidationResult | None


def event(db, operation):
    db.add(
        OperationEvent(operation_id=operation.id, version=operation.version, state=operation.state)
    )


@router.post(
    "/environments/{environment_id}/validate", response_model=OperationView, status_code=202
)
async def enqueue_validation(
    environment_id: uuid.UUID, body: ValidationRequest, actor: Actor, db: DB, request: Request
):
    require_role(actor, "TEACHER")
    # Serialize against policy changes and other submissions by the same actor.
    user = await db.scalar(
        select(User)
        .where(User.id == actor.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if user.status != "ACTIVE" or not await db.get(UserRole, (actor.id, "TEACHER")):
        raise Problem(403, "FORBIDDEN", "Недостаточно прав.")
    environment = await db.scalar(
        select(Environment)
        .where(Environment.id == environment_id, Environment.owner_teacher_id == actor.id)
        .with_for_update()
    )
    if not environment:
        raise Problem(404, "NOT_FOUND", "Окружение не найдено.")
    digest = hashlib.sha256(
        f"{VALIDATE}:{environment_id}:{body.expected_version}".encode()
    ).hexdigest()
    previous = await db.scalar(
        select(Operation).where(
            Operation.actor_id == actor.id, Operation.request_id == body.request_id
        )
    )
    if previous:
        if previous.request_digest != digest:
            raise Problem(409, "IDEMPOTENCY_CONFLICT", "Этот запрос уже использован иначе.")
        return previous
    if environment.version != body.expected_version:
        raise Problem(409, "VERSION_CONFLICT", "Окружение изменилось. Обновите страницу.")
    if await db.scalar(
        select(Operation.id).where(
            Operation.environment_id == environment_id, Operation.state.not_in(TERMINAL)
        )
    ):
        raise Problem(409, "OPERATION_IN_PROGRESS", "Для окружения уже выполняется операция.")
    operation = Operation(
        actor_id=actor.id,
        owner_teacher_id=actor.id,
        environment_id=environment_id,
        kind=VALIDATE,
        request_id=body.request_id,
        request_digest=digest,
        expected_version=body.expected_version,
    )
    db.add(operation)
    await db.flush()
    event(db, operation)
    audit(db, request, actor.id, "operation.queued", operation.id, kind=VALIDATE)
    await db.commit()
    return operation


def scoped_query(actor):
    query = select(Operation)
    if "ADMIN" not in actor.roles:
        require_role(actor, "TEACHER")
        query = query.where(Operation.owner_teacher_id == actor.id)
    return query


@router.get("/operations", response_model=list[OperationView])
async def list_operations(
    actor: Actor,
    db: DB,
    environment_id: uuid.UUID | None = None,
    after: uuid.UUID | None = None,
    limit: int = Query(50, ge=1, le=100),
):
    query = (
        scoped_query(actor).order_by(Operation.created_at.desc(), Operation.id.desc()).limit(limit)
    )
    if environment_id:
        query = query.where(Operation.environment_id == environment_id)
    if after:
        cursor = await db.scalar(scoped_query(actor).where(Operation.id == after))
        if not cursor:
            raise Problem(404, "NOT_FOUND", "Операция не найдена.")
        query = query.where(
            or_(
                Operation.created_at < cursor.created_at,
                (Operation.created_at == cursor.created_at) & (Operation.id < cursor.id),
            )
        )
    return list(await db.scalars(query))


@router.get("/operations/{operation_id}", response_model=OperationView)
async def get_operation(operation_id: uuid.UUID, actor: Actor, db: DB):
    operation = await db.scalar(scoped_query(actor).where(Operation.id == operation_id))
    if not operation:
        raise Problem(404, "NOT_FOUND", "Операция не найдена.")
    return operation


class WorkerStatus(BaseModel):
    state: Literal["AVAILABLE", "UNAVAILABLE"]
    last_seen_at: datetime | None


@router.get("/admin/worker-status", response_model=WorkerStatus)
async def worker_status(actor: Actor, db: DB):
    require_role(actor, "ADMIN")
    last_seen = await db.scalar(select(func.max(WorkerHeartbeat.seen_at)))
    now = await db.scalar(select(func.clock_timestamp()))
    return WorkerStatus(
        state="AVAILABLE"
        if last_seen and (now - last_seen).total_seconds() <= 30
        else "UNAVAILABLE",
        last_seen_at=last_seen,
    )
