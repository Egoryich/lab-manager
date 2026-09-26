"""Run browser scenarios against an isolated temporary PostgreSQL schema."""

import asyncio
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet
from lab_manager.config import Settings
from lab_manager.models import LocalCredential, TeacherPermissionOverride, User, UserRole
from lab_manager.security import PASSWORD_HASH
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


async def run():
    settings = Settings()
    if settings.environment not in {"development", "test"}:
        raise SystemExit("E2E refuses production settings")
    schema = "lab_test_" + uuid.uuid4().hex
    admin_engine = create_async_engine(settings.database_url.get_secret_value())
    api_process = None
    engine = None
    Path("artifacts").mkdir(exist_ok=True)
    try:
        async with admin_engine.begin() as db:
            await db.execute(text(f'CREATE SCHEMA "{schema}"'))
        url = (
            make_url(settings.database_url.get_secret_value())
            .update_query_dict({"options": f"-csearch_path={schema}"})
            .render_as_string(hide_password=False)
        )
        config = Config("alembic.ini")
        config.attributes["database_url"] = url
        await asyncio.to_thread(command.upgrade, config, "head")
        engine = create_async_engine(url)
        password = secrets.token_urlsafe(24)
        async with async_sessionmaker(engine)() as db, db.begin():
            for role in ("ADMIN", "TEACHER"):
                user = User(username=role.lower() + ".e2e", display_name=role.capitalize() + " E2E")
                db.add(user)
                await db.flush()
                db.add(UserRole(user_id=user.id, role=role))
                db.add(LocalCredential(user_id=user.id, password_hash=PASSWORD_HASH.hash(password)))
                if role == "TEACHER":
                    db.add(
                        TeacherPermissionOverride(
                            teacher_id=user.id, key="can_create_groups", allowed=True
                        )
                    )
        env = os.environ | {
            "LAB_DATABASE_URL": url,
            "LAB_ENVIRONMENT": "test",
            "LAB_PUBLIC_ORIGIN": "http://localhost:5174",
            "LAB_ENCRYPTION_KEY": Fernet.generate_key().decode(),
            "LAB_DIGEST_KEY": secrets.token_hex(48),
            "LAB_E2E_PASSWORD": password,
        }
        # The application launcher also selects the psycopg-compatible Windows loop.
        with Path("artifacts/e2e-api.log").open("w") as log:
            api_process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "lab_manager",
                    "--port",
                    "8001",
                ],
                env=env,
                stdout=log,
                stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            deadline = time.monotonic() + 30
            last_status = "no response"
            async with httpx.AsyncClient(trust_env=False) as client:
                while True:
                    if api_process.poll() is not None:
                        raise RuntimeError("E2E API failed; see artifacts/e2e-api.log")
                    try:
                        response = await client.get("http://127.0.0.1:8001/api/health/ready")
                        last_status = f"HTTP {response.status_code}: {response.text[:200]}"
                        if response.status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    if time.monotonic() > deadline:
                        raise RuntimeError(f"E2E API readiness timed out: {last_status}")
                    await asyncio.sleep(0.25)
            result = await asyncio.to_thread(
                subprocess.run,
                [shutil.which("npm.cmd" if os.name == "nt" else "npm"), "run", "test:e2e"],
                env=env,
                check=False,
            )
            return result.returncode
    finally:
        if api_process:
            api_process.terminate()
            api_process.wait(timeout=15)
        if engine:
            await engine.dispose()
        assert re.fullmatch(r"lab_test_[a-f0-9]{32}", schema)
        async with admin_engine.begin() as db:
            await db.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin_engine.dispose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
