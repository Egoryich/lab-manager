import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Query, Request
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from lab_manager.audit import audit
from lab_manager.dependencies import DB, Actor, Problem, can_create_groups, require_role, throttle
from lab_manager.models import Group, GroupJoinCode, GroupMember, User
from lab_manager.schemas import GroupCreate, GroupEdit, GroupJoin, GroupView, UserView, VersionInput
from lab_manager.security import new_join_code

router = APIRouter(prefix="/groups", tags=["Groups"])


async def managed_group(db, actor, group_id, *, lock=False):
    query = select(Group).where(Group.id == group_id)
    if "ADMIN" not in actor.roles:
        query = query.where(Group.owner_teacher_id == actor.id)
        require_role(actor, "TEACHER")
    if lock:
        query = query.with_for_update()
    group = await db.scalar(query)
    if not group:
        raise Problem(404, "NOT_FOUND", "Группа не найдена.")
    return group


async def view(db, codec, group, managed):
    code, count = None, None
    if managed:
        encrypted = await db.scalar(
            select(GroupJoinCode.ciphertext).where(
                GroupJoinCode.group_id == group.id, GroupJoinCode.revoked_at.is_(None)
            )
        )
        code = codec.decrypt(encrypted) if encrypted else None
        count = await db.scalar(
            select(func.count())
            .select_from(GroupMember)
            .where(GroupMember.group_id == group.id, GroupMember.status == "ACTIVE")
        )
    return GroupView(
        id=group.id,
        name=group.name,
        version=group.version,
        join_enabled=group.join_enabled,
        join_code=code,
        member_count=count,
    )


async def issue_code(db, codec, group):
    for _ in range(8):
        code = new_join_code()
        try:
            async with db.begin_nested():
                db.add(
                    GroupJoinCode(
                        group_id=group.id,
                        digest=codec.digest("join", code),
                        ciphertext=codec.encrypt(code),
                    )
                )
                await db.flush()
            return
        except IntegrityError as error:
            if getattr(error.orig.diag, "constraint_name", "") != "uq_active_code_digest":
                raise
    raise Problem(503, "CODE_UNAVAILABLE", "Не удалось создать код. Повторите позже.")


@router.post("", response_model=GroupView, status_code=201)
async def create_group(body: GroupCreate, actor: Actor, request: Request, db: DB):
    # Admin grants Teacher explicitly; creating a group always gives it a teacher owner.
    if not await can_create_groups(db, actor):
        raise Problem(403, "FORBIDDEN", "Создание групп не разрешено администратором.")
    await throttle(request, "create-group", str(actor.id), 30, 600)
    group = Group(name=body.name, owner_teacher_id=actor.id)
    db.add(group)
    await db.flush()
    await issue_code(db, request.app.state.codec, group)
    audit(db, request, actor.id, "group.created", group.id)
    result = await view(db, request.app.state.codec, group, True)
    await db.commit()
    return result


@router.get("", response_model=list[GroupView])
async def list_groups(
    actor: Actor,
    request: Request,
    db: DB,
    limit: int = Query(50, ge=1, le=100),
    after: uuid.UUID | None = None,
):
    query = select(Group).order_by(Group.id).limit(limit)
    if "ADMIN" not in actor.roles:
        membership = select(GroupMember.group_id).where(
            GroupMember.student_id == actor.id, GroupMember.status == "ACTIVE"
        )
        query = query.where((Group.owner_teacher_id == actor.id) | Group.id.in_(membership))
    if after:
        query = query.where(Group.id > after)
    groups = list(await db.scalars(query))
    return [
        await view(
            db,
            request.app.state.codec,
            group,
            "ADMIN" in actor.roles or group.owner_teacher_id == actor.id,
        )
        for group in groups
    ]


@router.post("/join", response_model=GroupView)
async def join_group(body: GroupJoin, actor: Actor, request: Request, db: DB):
    require_role(actor, "STUDENT")
    await throttle(request, "join", str(actor.id), 10, 300)
    digest = request.app.state.codec.digest("join", body.code)
    group_id = await db.scalar(
        select(GroupJoinCode.group_id).where(
            GroupJoinCode.digest == digest, GroupJoinCode.revoked_at.is_(None)
        )
    )
    group = await db.scalar(select(Group).where(Group.id == group_id).with_for_update())
    # Recheck after lock: rotation/disable and joining serialize on the group row.
    current = await db.scalar(
        select(GroupJoinCode.id).where(
            GroupJoinCode.group_id == group_id,
            GroupJoinCode.digest == digest,
            GroupJoinCode.revoked_at.is_(None),
        )
    )
    if not group or not current or not group.join_enabled or group.archived_at:
        raise Problem(404, "JOIN_UNAVAILABLE", "Код недействителен или вступление закрыто.")
    member = await db.scalar(
        select(GroupMember).where(
            GroupMember.group_id == group.id, GroupMember.student_id == actor.id
        )
    )
    if member and member.status in {"BANNED", "REMOVING"}:
        raise Problem(404, "JOIN_UNAVAILABLE", "Код недействителен или вступление закрыто.")
    if not member:
        db.add(GroupMember(group_id=group.id, student_id=actor.id))
        audit(db, request, actor.id, "group.joined", group.id)
        group.version += 1
    elif member.status == "REMOVED":
        member.status = "ACTIVE"
        member.generation += 1
        member.joined_at = datetime.now(UTC)
        member.removed_at = None
        group.version += 1
        audit(db, request, actor.id, "group.rejoined", group.id, generation=member.generation)
    result = await view(db, request.app.state.codec, group, False)
    await db.commit()
    return result


@router.get("/{group_id}", response_model=GroupView)
async def get_group(group_id: uuid.UUID, actor: Actor, request: Request, db: DB):
    group = await db.get(Group, group_id)
    managed = group and ("ADMIN" in actor.roles or group.owner_teacher_id == actor.id)
    member = await db.scalar(
        select(GroupMember.id).where(
            GroupMember.group_id == group_id,
            GroupMember.student_id == actor.id,
            GroupMember.status == "ACTIVE",
        )
    )
    if not group or not (managed or member):
        raise Problem(404, "NOT_FOUND", "Группа не найдена.")
    return await view(db, request.app.state.codec, group, managed)


@router.get("/{group_id}/members", response_model=list[UserView])
async def members(
    group_id: uuid.UUID,
    actor: Actor,
    db: DB,
    limit: int = Query(50, ge=1, le=100),
    after: uuid.UUID | None = None,
):
    await managed_group(db, actor, group_id)
    query = (
        select(User)
        .join(GroupMember, GroupMember.student_id == User.id)
        .where(GroupMember.group_id == group_id, GroupMember.status == "ACTIVE")
        .order_by(User.id)
        .limit(limit)
    )
    if after:
        query = query.where(User.id > after)
    return [
        UserView(
            id=user.id, username=user.username, display_name=user.display_name, roles=["STUDENT"]
        )
        for user in await db.scalars(query)
    ]


def check_version(group, expected):
    if group.version != expected:
        raise Problem(409, "VERSION_CONFLICT", "Группа изменилась. Обновите страницу.")


@router.patch("/{group_id}", response_model=GroupView)
async def edit_group(group_id: uuid.UUID, body: GroupEdit, actor: Actor, request: Request, db: DB):
    group = await managed_group(db, actor, group_id, lock=True)
    check_version(group, body.expected_version)
    if group.archived_at:
        raise Problem(409, "GROUP_ARCHIVED", "Группа закрыта.")
    group.join_enabled = body.join_enabled
    group.version += 1
    audit(db, request, actor.id, "group.join_changed", group.id, enabled=body.join_enabled)
    result = await view(db, request.app.state.codec, group, True)
    await db.commit()
    return result


@router.post("/{group_id}/join-code/regenerate", response_model=GroupView)
async def rotate_code(
    group_id: uuid.UUID, body: VersionInput, actor: Actor, request: Request, db: DB
):
    group = await managed_group(db, actor, group_id, lock=True)
    check_version(group, body.expected_version)
    old = await db.scalar(
        select(GroupJoinCode).where(
            GroupJoinCode.group_id == group_id, GroupJoinCode.revoked_at.is_(None)
        )
    )
    if old:
        old.revoked_at = datetime.now(UTC)
        await db.flush()
    await issue_code(db, request.app.state.codec, group)
    group.version += 1
    audit(db, request, actor.id, "group.code_rotated", group.id)
    result = await view(db, request.app.state.codec, group, True)
    await db.commit()
    return result
