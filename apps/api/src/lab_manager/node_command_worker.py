"""Process durable node receipts without ever replaying a submitted POST."""

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, or_, select, text

from lab_manager.node_command_models import NodeCommand
from lab_manager.node_commands import (
    NodeCommandClient,
    NodeCommandRejected,
    NodeCommandUnavailable,
    NodeCommandUncertain,
)

logger = logging.getLogger("lab_manager.node_commands")
LEASE_SECONDS = 45


@dataclass(frozen=True)
class CommandClaim:
    id: uuid.UUID
    fence: int
    owner: uuid.UUID
    node_id: uuid.UUID
    submit: bool
    prior_state: str
    payload: dict


async def claim(
    sessions, worker_id: uuid.UUID, node_ids: tuple[uuid.UUID, ...]
) -> CommandClaim | None:
    if not node_ids:
        return None
    async with sessions() as db, db.begin():
        await db.execute(text("SET LOCAL statement_timeout = '10s'"))
        await db.execute(text("SET LOCAL lock_timeout = '3s'"))
        now = await db.scalar(select(func.clock_timestamp()))
        row = await db.scalar(
            select(NodeCommand)
            .where(
                NodeCommand.state.in_(("QUEUED", "ATTEMPTED", "SUBMITTED")),
                NodeCommand.node_id.in_(node_ids),
                NodeCommand.available_at <= now,
                or_(NodeCommand.lease_until.is_(None), NodeCommand.lease_until <= now),
            )
            .order_by(NodeCommand.available_at, NodeCommand.created_at, NodeCommand.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if row is None:
            return None
        prior_state = row.state
        submit = row.state == "QUEUED"
        if submit:
            # Commit this fact before the network call. A crash can lose one command,
            # but can never silently submit it twice.
            row.state = "ATTEMPTED"
            row.submit_attempted_at = now
        row.fence += 1
        row.lease_owner = worker_id
        row.lease_until = now + timedelta(seconds=LEASE_SECONDS)
        return CommandClaim(
            row.id, row.fence, worker_id, row.node_id, submit, prior_state, row.payload
        )


def receipt_state(receipt: dict, claim: CommandClaim) -> tuple[str, str | None]:
    payload = claim.payload
    if (
        receipt["operation_id"] != str(claim.id)
        or receipt["runtime_id"] != payload["runtime_id"]
        or receipt["kind"] != payload["kind"]
        or receipt["vmid"] != payload["vmid"]
        or receipt["generation"] != payload["generation"]
    ):
        return "UNCERTAIN", "NODE_COMMAND_RECEIPT_MISMATCH"
    state = receipt["state"]
    if state == "SUCCEEDED":
        return "SUCCEEDED", None
    if state in ("FAILED", "UNCERTAIN"):
        # Even a failed Proxmox task can leave a guest or disk behind.
        return "UNCERTAIN", receipt["error_code"] or "NODE_RECONCILIATION_REQUIRED"
    if state in ("INTENT", "SUBMITTED"):
        return "SUBMITTED", None
    return "UNCERTAIN", "NODE_COMMAND_RECEIPT_INVALID"


async def store_result(
    sessions,
    claim: CommandClaim,
    *,
    state: str,
    receipt: dict | None = None,
    error_code: str | None = None,
) -> bool:
    async with sessions() as db, db.begin():
        await db.execute(text("SET LOCAL statement_timeout = '10s'"))
        row = await db.scalar(
            select(NodeCommand).where(NodeCommand.id == claim.id).with_for_update()
        )
        if (
            row is None
            or row.fence != claim.fence
            or row.lease_owner != claim.owner
            or row.state not in ("ATTEMPTED", "SUBMITTED")
        ):
            return False
        now = await db.scalar(select(func.clock_timestamp()))
        row.state = state
        row.receipt = receipt or row.receipt
        row.error_code = error_code
        row.lease_owner = row.lease_until = None
        row.available_at = now + timedelta(seconds=5 if state == "SUBMITTED" else 15)
        if state in ("SUCCEEDED", "FAILED", "UNCERTAIN"):
            row.finished_at = now
        return True


async def process(sessions, worker_id: uuid.UUID, endpoints) -> bool:
    ticket = await claim(sessions, worker_id, tuple(endpoint.id for endpoint in endpoints))
    if ticket is None:
        return False
    endpoint = next((item for item in endpoints if item.id == ticket.node_id), None)
    if endpoint is None:
        await store_result(
            sessions, ticket, state="UNCERTAIN", error_code="NODE_ENDPOINT_NOT_CONFIGURED"
        )
        return True
    client = NodeCommandClient(endpoint)
    try:
        if ticket.submit:
            receipt = await asyncio.to_thread(client.submit, ticket.payload)
        else:
            receipt = await asyncio.to_thread(client.status, ticket.id)
    except NodeCommandRejected as error:
        if ticket.submit:
            state = "FAILED"
        else:
            state = "UNCERTAIN"
        await store_result(sessions, ticket, state=state, error_code=error.code)
    except (NodeCommandUncertain, NodeCommandUnavailable) as error:
        state = "ATTEMPTED" if ticket.submit else ticket.prior_state
        await store_result(sessions, ticket, state=state, error_code=error.code)
    else:
        state, error = receipt_state(receipt, ticket)
        await store_result(sessions, ticket, state=state, receipt=receipt, error_code=error)
    return True
