"""Teacher booking: recheck permissions, roster and capacity in one transaction."""

import uuid
from datetime import datetime

from fastapi import APIRouter, Request
from pydantic import ConfigDict, Field
from sqlalchemy import select

from lab_manager.audit import audit
from lab_manager.catalog import configuration, estimate
from lab_manager.catalog_models import Environment
from lab_manager.catalog_schemas import EnvironmentCreate, MachineSizing
from lab_manager.dependencies import Actor, Problem, require_role
from lab_manager.lesson_preview import demand_from_total
from lab_manager.reservation_models import LessonReservation
from lab_manager.reservations import AdmissionRejected, cancel_future_lesson, reserve_lesson
from lab_manager.schemas import Input

router = APIRouter(tags=["Lessons"])


class BookLessonRequest(Input):
    request_id: uuid.UUID
    node_id: uuid.UUID
    expected_environment_version: int = Field(ge=1)
    expected_group_version: int = Field(ge=1)
    starts_at: datetime
    ends_at: datetime


class ReservationView(Input):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    environment_id: uuid.UUID
    node_id: uuid.UUID
    starts_at: datetime
    ends_at: datetime
    state: str
    memory_mib: int
    cpu_millicredits: int
    disk_bytes: int
    hibernation_bytes: int


def environment_body(environment: Environment) -> EnvironmentCreate:
    return EnvironmentCreate(
        name=environment.name,
        group_id=environment.group_id,
        profile_version_id=environment.profile_version_id,
        demo_profile_version_id=environment.demo_profile_version_id,
        request_id=environment.request_id,
        student_resources=MachineSizing(
            memory_mib=environment.student_memory_mib,
            vcpu=environment.student_vcpu,
            disk_gib=environment.student_disk_gib,
        ),
        demo_resources=MachineSizing(
            memory_mib=environment.demo_memory_mib,
            vcpu=environment.demo_vcpu,
            disk_gib=environment.demo_disk_gib,
        ),
    )


@router.get("/environments/{environment_id}/reservations", response_model=list[ReservationView])
async def list_reservations(environment_id: uuid.UUID, actor: Actor, request: Request):
    require_role(actor, "TEACHER")
    async with request.app.state.sessions() as db:
        owned = await db.scalar(
            select(Environment.id).where(
                Environment.id == environment_id,
                Environment.owner_teacher_id == actor.id,
            )
        )
        if owned is None:
            raise Problem(404, "NOT_FOUND", "Окружение не найдено.")
        return list(
            await db.scalars(
                select(LessonReservation)
                .where(
                    LessonReservation.environment_id == environment_id,
                    LessonReservation.state.in_(("RESERVED", "ACTIVE")),
                )
                .order_by(LessonReservation.starts_at)
                .limit(100)
            )
        )


@router.post(
    "/environments/{environment_id}/reservations",
    response_model=ReservationView,
    status_code=201,
)
async def book_lesson(
    environment_id: uuid.UUID,
    body: BookLessonRequest,
    actor: Actor,
    request: Request,
):
    require_role(actor, "TEACHER")

    async def preflight(transaction):
        environment = await transaction.scalar(
            select(Environment).where(
                Environment.id == environment_id,
                Environment.owner_teacher_id == actor.id,
            )
        )
        if environment is None:
            raise Problem(404, "NOT_FOUND", "Окружение не найдено.")
        group, policy, selected, student_size, demo_size = await configuration(
            transaction, actor, environment_body(environment), lock=True
        )
        await transaction.refresh(environment, with_for_update=True)
        if (
            environment.owner_teacher_id != actor.id
            or environment.version != body.expected_environment_version
            or group.version != body.expected_group_version
        ):
            raise Problem(409, "VERSION_CONFLICT", "Группа или окружение изменились.")
        plan = await estimate(transaction, group, *selected, policy, student_size, demo_size)
        if plan.student_count < 1:
            raise Problem(409, "GROUP_EMPTY", "В группе пока нет студентов.")
        if not plan.within_per_environment_limits:
            raise Problem(409, "QUOTA_EXCEEDED", "Превышен лимит окружения.")
        return demand_from_total(plan.total)

    async def on_reserved(transaction, reservation):
        audit(
            transaction,
            request,
            actor.id,
            "lesson.reserved",
            reservation.id,
            environment_id=str(environment_id),
            node_id=str(body.node_id),
        )

    try:
        reservation = await reserve_lesson(
            request.app.state.sessions,
            node_id=body.node_id,
            environment_id=environment_id,
            teacher_id=actor.id,
            request_id=body.request_id,
            starts_at=body.starts_at,
            ends_at=body.ends_at,
            demand=None,
            preflight=preflight,
            on_reserved=on_reserved,
        )
    except AdmissionRejected as error:
        raise Problem(409, error.reasons[0], "Занятие сейчас нельзя забронировать.") from error
    return reservation


@router.post("/reservations/{reservation_id}/cancel", response_model=ReservationView)
async def cancel_reservation(reservation_id: uuid.UUID, actor: Actor, request: Request):
    require_role(actor, "TEACHER")

    async def on_cancel(transaction, reservation):
        audit(
            transaction,
            request,
            actor.id,
            "lesson.reservation_cancelled",
            reservation.id,
            environment_id=str(reservation.environment_id),
        )

    try:
        return await cancel_future_lesson(
            request.app.state.sessions,
            reservation_id=reservation_id,
            teacher_id=actor.id,
            on_cancel=on_cancel,
        )
    except AdmissionRejected as error:
        code = error.reasons[0]
        status = 404 if code == "RESERVATION_NOT_FOUND" else 409
        raise Problem(status, code, "Бронь нельзя отменить.") from error
