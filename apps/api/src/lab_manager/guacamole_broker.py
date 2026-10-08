"""Atomic one-time launch exchange for the private Guacamole gateway."""

import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from lab_manager.access_models import AccessGrant
from lab_manager.catalog_models import Environment
from lab_manager.dependencies import Problem, throttle
from lab_manager.guacamole_auth import GuacamoleGrantError, SSHConnection
from lab_manager.models import BrowserSession, Group, GroupMember, User, UserRole
from lab_manager.network_models import NetworkSegmentAllocation
from lab_manager.reservation_models import LessonReservation
from lab_manager.runtime_access import may_open
from lab_manager.runtime_models import EnvironmentRun, RunRuntime, Runtime, RuntimeSshCredential

router = APIRouter(tags=["Gateway"])


class ConsumeLaunch(BaseModel):
    nonce: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")


class GatewayConnection(BaseModel):
    grant_id: uuid.UUID
    user_id: uuid.UUID
    browser_session_id: uuid.UUID
    runtime_id: uuid.UUID
    environment_run_id: uuid.UUID
    valid_until: datetime
    protocol: str
    parameters: dict[str, str]


def valid_gateway_signature(
    secret_hex: str, timestamp: str, nonce: str, signature: str, now: datetime
) -> bool:
    """Authenticate the gateway request without sending its long-lived key."""
    try:
        second = int(timestamp)
        if abs(now.timestamp() - second) > 30:
            return False
        if not isinstance(nonce, str) or not isinstance(signature, str):
            return False
        if len(signature) != 64:
            return False
        expected = hmac.new(
            bytes.fromhex(secret_hex), f"{second}:{nonce}".encode(), hashlib.sha256
        ).hexdigest()
    except (TypeError, ValueError):
        return False
    return secrets.compare_digest(signature.lower(), expected)


@router.post(
    "/internal/guacamole/consume",
    response_model=GatewayConnection,
    include_in_schema=False,
)
async def consume_launch(body: ConsumeLaunch, request: Request):
    settings = request.app.state.settings
    secret = settings.guacamole_broker_secret
    if not settings.guacamole_broker_enabled or secret is None:
        raise Problem(503, "GUACAMOLE_NOT_CONFIGURED", "Терминал пока недоступен.")
    now_header = request.headers.get("x-lab-guacamole-time", "")
    signature = request.headers.get("x-lab-guacamole-signature", "")
    # Reject forged requests before touching Redis or PostgreSQL.
    if not valid_gateway_signature(
        secret.get_secret_value(), now_header, body.nonce, signature, datetime.now(UTC)
    ):
        raise Problem(401, "GATEWAY_AUTH_FAILED", "Доступ запрещён.")
    await throttle(request, "guacamole_consume", "gateway", 120, 60)

    async with request.app.state.sessions() as db, db.begin():
        grant = await db.scalar(
            select(AccessGrant)
            .where(
                AccessGrant.nonce_digest
                == request.app.state.codec.digest("guacamole_launch", body.nonce)
            )
            .with_for_update()
        )
        now = await db.scalar(select(func.clock_timestamp()))
        if (
            grant is None
            or grant.expires_at <= now
            or grant.consumed_at is not None
            or grant.revoked_at is not None
        ):
            raise Problem(401, "LAUNCH_INVALID", "Доступ запрещён.")
        browser_session = await db.get(BrowserSession, grant.browser_session_id)
        user = await db.get(User, grant.user_id)
        runtime = await db.get(Runtime, grant.runtime_id)
        run = await db.get(EnvironmentRun, grant.environment_run_id)
        if browser_session is None or user is None or runtime is None or run is None:
            raise Problem(401, "LAUNCH_REVOKED", "Доступ запрещён.")
        environment = await db.get(Environment, runtime.environment_id)
        group = await db.get(Group, environment.group_id) if environment else None
        roles = list(await db.scalars(select(UserRole.role).where(UserRole.user_id == user.id)))
        membership = None
        if group and runtime.student_id == user.id:
            membership = await db.scalar(
                select(GroupMember).where(
                    GroupMember.group_id == group.id, GroupMember.student_id == user.id
                )
            )
        roster = await db.get(RunRuntime, (run.id, runtime.id))
        reservation = await db.get(LessonReservation, run.reservation_id)
        allocation = (
            await db.get(NetworkSegmentAllocation, runtime.network_allocation_id)
            if runtime.network_allocation_id
            else None
        )
        credential = await db.get(RuntimeSshCredential, runtime.id)
        actor = SimpleNamespace(id=user.id, roles=roles)
        if (
            browser_session.user_id != user.id
            or browser_session.revoked_at is not None
            or browser_session.expires_at <= now
            or browser_session.auth_revision != user.auth_revision
            or user.status != "ACTIVE"
            or environment is None
            or group is None
            or group.archived_at is not None
            or not may_open(actor, runtime, environment, membership)
            or run.environment_id != environment.id
            or run.node_id != runtime.node_id
            or run.state != "RUNNING"
            or roster is None
            or roster.environment_id != environment.id
            or roster.state != "ACTIVE"
            or reservation is None
            or reservation.environment_id != environment.id
            or reservation.node_id != runtime.node_id
            or reservation.state != "ACTIVE"
            or reservation.starts_at > now
            or reservation.ends_at <= now
            or runtime.kind != "LXC"
            or runtime.state != "RUNNING"
            or allocation is None
            or allocation.environment_id != environment.id
            or allocation.node_id != runtime.node_id
            or allocation.state != "APPLIED"
            or credential is None
            or not credential.host_key
            or not runtime.guest_ipv4
        ):
            raise Problem(401, "LAUNCH_REVOKED", "Доступ запрещён.")
        try:
            connection = SSHConnection(
                runtime_id=runtime.id,
                address=runtime.guest_ipv4,
                subnet=allocation.cidr,
                username="root",
                private_key=request.app.state.codec.decrypt(credential.private_key_ciphertext),
                host_key=credential.host_key,
            ).payload()
        except (GuacamoleGrantError, InvalidToken) as error:
            raise Problem(503, "CONNECTION_NOT_READY", "Терминал пока недоступен.") from error
        grant.consumed_at = now
        return GatewayConnection(
            grant_id=grant.id,
            user_id=user.id,
            browser_session_id=browser_session.id,
            runtime_id=runtime.id,
            environment_run_id=run.id,
            valid_until=min(reservation.ends_at, browser_session.expires_at),
            protocol=connection["protocol"],
            parameters=connection["parameters"],
        )
