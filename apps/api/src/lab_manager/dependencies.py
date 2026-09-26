import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, Request
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lab_manager.models import BrowserSession, User, UserRole
from lab_manager.schemas import UserView


class Problem(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message


async def database(request: Request):
    async with request.app.state.sessions() as db:
        yield db


DB = Annotated[AsyncSession, Depends(database)]


@dataclass
class Identity:
    user: User
    roles: list[str]
    session: BrowserSession
    csrf_token: str

    @property
    def id(self) -> uuid.UUID:
        return self.user.id

    def view(self) -> UserView:
        return UserView(
            id=self.id,
            username=self.user.username,
            display_name=self.user.display_name,
            roles=self.roles,
        )


async def identity(request: Request, db: DB) -> Identity:
    token = request.cookies.get(request.app.state.settings.cookie_name, "")
    digest = request.app.state.codec.digest("session", token)
    pair = (
        await db.execute(
            select(BrowserSession, User)
            .join(User, User.id == BrowserSession.user_id)
            .where(
                BrowserSession.token_digest == digest,
                BrowserSession.revoked_at.is_(None),
                BrowserSession.expires_at > datetime.now(UTC),
                BrowserSession.auth_revision == User.auth_revision,
                User.status == "ACTIVE",
            )
        )
    ).first()
    if pair is None:
        raise Problem(401, "AUTH_REQUIRED", "Войдите в систему.")
    session, user = pair
    roles = list(await db.scalars(select(UserRole.role).where(UserRole.user_id == user.id)))
    csrf = request.app.state.codec.digest("csrf", token)
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        if not secrets.compare_digest(request.headers.get("x-csrf-token", ""), csrf):
            raise Problem(403, "CSRF_FAILED", "Обновите страницу и повторите действие.")
    return Identity(user, roles, session, csrf)


Actor = Annotated[Identity, Depends(identity)]


def require_role(actor: Identity, role: str):
    if role not in actor.roles:
        raise Problem(403, "FORBIDDEN", "Недостаточно прав.")


async def can_create_groups(db: AsyncSession, actor: Identity) -> bool:
    if "TEACHER" not in actor.roles:
        return False
    from lab_manager.permissions import effective_policy

    return (await effective_policy(db, actor.id)).permissions["can_create_groups"]


RATE_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return count
"""


async def throttle(request: Request, bucket: str, subject: str, limit: int, seconds: int):
    key = "lab:rate:" + request.app.state.codec.digest(bucket, subject)
    try:
        count = await request.app.state.redis.eval(RATE_SCRIPT, 1, key, seconds)
    except RedisError as error:
        raise Problem(503, "RATE_LIMIT_UNAVAILABLE", "Сервис временно недоступен.") from error
    if count > limit:
        raise Problem(429, "RATE_LIMITED", "Слишком много попыток. Попробуйте позже.")
