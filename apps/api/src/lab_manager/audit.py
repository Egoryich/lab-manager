import uuid

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from lab_manager.models import AuditEvent


def audit(
    db: AsyncSession,
    request: Request,
    actor_id: uuid.UUID,
    action: str,
    target_id: uuid.UUID,
    **details,
):
    # Call sites pass only explicit non-secret fields; never serialize request bodies.
    db.add(
        AuditEvent(
            actor_id=actor_id,
            action=action,
            target_id=target_id,
            request_id=request.state.request_id,
            details=details,
        )
    )
