"""One-time browser launches for the Guacamole authentication extension."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from lab_manager.models import Base


class AccessGrant(Base):
    __tablename__ = "access_grants"
    __table_args__ = (
        Index("ix_access_grants_browser_runtime", "browser_session_id", "runtime_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    nonce_digest: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    browser_session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("browser_sessions.id"))
    runtime_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runtimes.id"))
    environment_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("environment_runs.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
