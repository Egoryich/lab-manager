import secrets
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Query, Request, Response
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from lab_manager.audit import audit
from lab_manager.dependencies import (
    DB,
    Actor,
    Problem,
    can_create_groups,
    require_role,
    throttle,
)
from lab_manager.models import (
    BrowserSession,
    LocalCredential,
    PasswordResetGrant,
    TeacherPermissionOverride,
    User,
    UserRole,
)
from lab_manager.schemas import (
    GrantTeacher,
    Login,
    Register,
    ResetGrantView,
    ResetPassword,
    SessionView,
    UserView,
)
from lab_manager.security import DUMMY_HASH, PASSWORD_HASH

router = APIRouter(tags=["Identity"])


async def password_hash(password: str, request: Request) -> str:
    async with request.app.state.password_slots:
        return await run_in_threadpool(PASSWORD_HASH.hash, password)


@router.post("/auth/register", response_model=UserView, status_code=201)
async def register(body: Register, request: Request, db: DB):
    # A whole classroom may share one NAT address.
    await throttle(request, "register", request.client.host, 100, 3600)
    user = User(username=body.username, display_name=body.display_name)
    db.add(user)
    try:
        await db.flush()
        db.add(UserRole(user_id=user.id, role="STUDENT"))
        db.add(
            LocalCredential(
                user_id=user.id,
                password_hash=await password_hash(body.password.get_secret_value(), request),
            )
        )
        audit(db, request, user.id, "user.registered", user.id)
        await db.commit()
    except IntegrityError as error:
        await db.rollback()
        raise Problem(
            409, "REGISTRATION_UNAVAILABLE", "Не удалось зарегистрировать этот логин."
        ) from error
    return UserView(
        id=user.id, username=user.username, display_name=user.display_name, roles=["STUDENT"]
    )


@router.post("/auth/login", response_model=SessionView)
async def login(body: Login, request: Request, response: Response, db: DB):
    await throttle(request, "login-ip", request.client.host, 100, 600)
    await throttle(request, "login-user", body.username, 10, 600)
    pair = (
        await db.execute(
            select(User, LocalCredential)
            .join(LocalCredential)
            .where(User.username == body.username)
        )
    ).first()
    stored = pair[1].password_hash if pair else DUMMY_HASH
    async with request.app.state.password_slots:
        valid = await run_in_threadpool(
            PASSWORD_HASH.verify, body.password.get_secret_value(), stored
        )
    if not pair or not valid or pair[0].status != "ACTIVE":
        raise Problem(401, "INVALID_CREDENTIALS", "Неверный логин или пароль.")
    user = pair[0]
    # Serialize login with recovery and role changes; reject verification against an old hash.
    await db.refresh(user, with_for_update=True)
    await db.refresh(pair[1])
    if pair[1].password_hash != stored or user.status != "ACTIVE":
        raise Problem(401, "INVALID_CREDENTIALS", "Неверный логин или пароль.")
    token = secrets.token_urlsafe(32)
    settings, codec = request.app.state.settings, request.app.state.codec
    roles = list(await db.scalars(select(UserRole.role).where(UserRole.user_id == user.id)))
    from lab_manager.permissions import effective_policy

    permission = (await effective_policy(db, user.id)).permissions["can_create_groups"]
    db.add(
        BrowserSession(
            user_id=user.id,
            token_digest=codec.digest("session", token),
            auth_revision=user.auth_revision,
            expires_at=datetime.now(UTC) + timedelta(hours=settings.session_hours),
        )
    )
    # Replace any existing browser session instead of leaving an unused live session.
    old = request.cookies.get(settings.cookie_name)
    if old:
        await db.execute(
            update(BrowserSession)
            .where(BrowserSession.token_digest == codec.digest("session", old))
            .values(revoked_at=datetime.now(UTC))
        )
    audit(db, request, user.id, "session.created", user.id)
    await db.commit()
    response.set_cookie(
        settings.cookie_name,
        token,
        httponly=True,
        secure=settings.environment == "production",
        samesite="lax",
        max_age=settings.session_hours * 3600,
        path="/",
    )
    return SessionView(
        user=UserView(
            id=user.id, username=user.username, display_name=user.display_name, roles=roles
        ),
        csrf_token=codec.digest("csrf", token),
        can_create_groups="TEACHER" in roles and permission,
    )


@router.get("/auth/me", response_model=SessionView)
async def me(actor: Actor, db: DB):
    return SessionView(
        user=actor.view(),
        csrf_token=actor.csrf_token,
        can_create_groups=await can_create_groups(db, actor),
    )


@router.post("/auth/logout", status_code=204)
async def logout(actor: Actor, request: Request, response: Response, db: DB):
    actor.session.revoked_at = datetime.now(UTC)
    audit(db, request, actor.id, "session.revoked", actor.session.id)
    await db.commit()
    response.delete_cookie(
        request.app.state.settings.cookie_name,
        path="/",
        secure=request.app.state.settings.environment == "production",
        httponly=True,
        samesite="lax",
    )


@router.get("/admin/users", response_model=list[UserView])
async def users(
    actor: Actor, db: DB, limit: int = Query(50, ge=1, le=100), after: uuid.UUID | None = None
):
    require_role(actor, "ADMIN")
    query = select(User).order_by(User.id).limit(limit)
    if after:
        query = query.where(User.id > after)
    result = list(await db.scalars(query))
    roles = (
        (
            await db.execute(
                select(UserRole).where(UserRole.user_id.in_([user.id for user in result]))
            )
        )
        .scalars()
        .all()
    )
    return [
        UserView(
            id=user.id,
            username=user.username,
            display_name=user.display_name,
            roles=[r.role for r in roles if r.user_id == user.id],
        )
        for user in result
    ]


@router.put("/admin/users/{user_id}/teacher", response_model=UserView)
async def grant_teacher(
    user_id: uuid.UUID, body: GrantTeacher, actor: Actor, request: Request, db: DB
):
    require_role(actor, "ADMIN")
    user = await db.scalar(select(User).where(User.id == user_id).with_for_update())
    if not user or user.status != "ACTIVE":
        raise Problem(404, "NOT_FOUND", "Пользователь не найден.")
    if not await db.get(UserRole, (user_id, "TEACHER")):
        db.add(UserRole(user_id=user_id, role="TEACHER"))
    permission = await db.get(TeacherPermissionOverride, (user_id, "can_create_groups"))
    if permission:
        permission.allowed = body.can_create_groups
    else:
        db.add(
            TeacherPermissionOverride(
                teacher_id=user_id, key="can_create_groups", allowed=body.can_create_groups
            )
        )
    user.auth_revision += 1
    audit(
        db,
        request,
        actor.id,
        "teacher.group_permission_set",
        user_id,
        can_create_groups=body.can_create_groups,
    )
    await db.commit()
    roles = list(await db.scalars(select(UserRole.role).where(UserRole.user_id == user_id)))
    return UserView(id=user.id, username=user.username, display_name=user.display_name, roles=roles)


@router.post("/admin/users/{user_id}/password-reset", response_model=ResetGrantView)
async def issue_reset(user_id: uuid.UUID, actor: Actor, request: Request, db: DB):
    require_role(actor, "ADMIN")
    await throttle(request, "reset-issue", str(actor.id), 20, 600)
    user = await db.scalar(select(User).where(User.id == user_id).with_for_update())
    if not user or user.status != "ACTIVE":
        raise Problem(404, "NOT_FOUND", "Пользователь не найден.")
    now = datetime.now(UTC)
    await db.execute(
        update(PasswordResetGrant)
        .where(PasswordResetGrant.user_id == user_id, PasswordResetGrant.used_at.is_(None))
        .values(used_at=now)
    )
    user.auth_revision += 1
    token = secrets.token_urlsafe(32)
    db.add(
        PasswordResetGrant(
            user_id=user_id,
            issued_by=actor.id,
            token_digest=request.app.state.codec.digest("reset", token),
            expires_at=now + timedelta(minutes=15),
        )
    )
    audit(db, request, actor.id, "password_reset.issued", user_id)
    await db.commit()
    return ResetGrantView(token=token, expires_in_seconds=900)


@router.post("/auth/reset-password", status_code=204)
async def reset_password(body: ResetPassword, request: Request, db: DB):
    await throttle(request, "reset-consume", request.client.host, 20, 600)
    digest = request.app.state.codec.digest("reset", body.token.get_secret_value())
    grant = await db.scalar(
        select(PasswordResetGrant).where(PasswordResetGrant.token_digest == digest)
    )
    if not grant:
        raise Problem(400, "INVALID_RESET", "Код восстановления недействителен или истёк.")
    user = await db.scalar(select(User).where(User.id == grant.user_id).with_for_update())
    await db.refresh(grant, with_for_update=True)
    if grant.used_at or grant.expires_at <= datetime.now(UTC) or user.status != "ACTIVE":
        raise Problem(400, "INVALID_RESET", "Код восстановления недействителен или истёк.")
    credential = await db.get(LocalCredential, user.id)
    credential.password_hash = await password_hash(body.password.get_secret_value(), request)
    credential.updated_at = datetime.now(UTC)
    grant.used_at = datetime.now(UTC)
    user.auth_revision += 1
    audit(db, request, user.id, "password_reset.consumed", user.id)
    await db.commit()
