import asyncio
import os
import re
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet
from lab_manager.config import Settings
from lab_manager.main import create_app
from lab_manager.models import (
    BrowserSession,
    LocalCredential,
    TeacherPermissionOverride,
    User,
    UserRole,
)
from lab_manager.security import PASSWORD_HASH
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

PASSWORD = "test-password-with-entropy"
HASH = PASSWORD_HASH.hash(PASSWORD)


@pytest.fixture
async def app():
    if os.getenv("LAB_RUN_INTEGRATION") != "1":
        pytest.skip(
            "Set LAB_RUN_INTEGRATION=1; tests create isolated lab_test_* PostgreSQL schemas"
        )
    base = Settings()
    if base.environment not in {"development", "test"}:
        pytest.fail("Integration tests refuse production settings")
    schema = "lab_test_" + uuid.uuid4().hex
    admin_engine = create_async_engine(
        base.database_url.get_secret_value(), connect_args={"connect_timeout": 5}
    )
    async with admin_engine.begin() as db:
        await db.execute(text(f'CREATE SCHEMA "{schema}"'))
    url = (
        make_url(base.database_url.get_secret_value())
        .update_query_dict({"options": f"-csearch_path={schema}"})
        .render_as_string(hide_password=False)
    )
    config = Config("alembic.ini")
    config.attributes["database_url"] = url
    application = None
    try:
        await asyncio.to_thread(command.upgrade, config, "head")
        settings = Settings(
            _env_file=None,
            environment="test",
            database_url=url,
            redis_url=base.redis_url,
            public_origin="http://localhost:5173",
            encryption_key=Fernet.generate_key().decode(),
            digest_key=secrets.token_hex(48),
        )
        application = create_app(settings)
        async with application.router.lifespan_context(application):
            yield application
    finally:
        # Only this fixture's generated schema is dropped, never public or an existing database.
        assert re.fullmatch(r"lab_test_[a-f0-9]{32}", schema)
        async with admin_engine.begin() as db:
            await db.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin_engine.dispose()


@pytest.fixture
def client_factory(app):
    @asynccontextmanager
    async def factory():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost:5173",
            headers={"Origin": "http://localhost:5173"},
        ) as client:
            yield client

    return factory


@pytest.fixture
def seed(app):
    async def create(role="STUDENT", permission=True):
        async with app.state.sessions() as db, db.begin():
            user = User(username="u" + uuid.uuid4().hex, display_name="Тестовый пользователь")
            db.add(user)
            await db.flush()
            db.add(UserRole(user_id=user.id, role=role))
            db.add(LocalCredential(user_id=user.id, password_hash=HASH))
            if role == "TEACHER":
                db.add(
                    TeacherPermissionOverride(
                        teacher_id=user.id, key="can_create_groups", allowed=permission
                    )
                )
        return user

    return create


@pytest.fixture
def session(app):
    async def create(client, user):
        # Most API tests focus on scope/concurrency; real login is covered separately.
        token = secrets.token_urlsafe(32)
        async with app.state.sessions() as db, db.begin():
            db.add(
                BrowserSession(
                    user_id=user.id,
                    token_digest=app.state.codec.digest("session", token),
                    auth_revision=user.auth_revision,
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )
        client.cookies.set("lab_session", token)
        client.headers["X-CSRF-Token"] = app.state.codec.digest("csrf", token)

    return create
