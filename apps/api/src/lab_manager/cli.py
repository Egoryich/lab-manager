"""Local bootstrap; no public endpoint can create an administrator."""

import argparse
import asyncio
import getpass
import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from lab_manager.config import Settings
from lab_manager.models import AuditEvent, LocalCredential, User, UserRole
from lab_manager.schemas import Register
from lab_manager.security import PASSWORD_HASH


async def bootstrap(body: Register):
    engine = create_async_engine(Settings().database_url.get_secret_value(), hide_parameters=True)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db, db.begin():
            await db.execute(text("SELECT pg_advisory_xact_lock(78491101)"))
            if await db.scalar(select(UserRole.user_id).where(UserRole.role == "ADMIN").limit(1)):
                raise SystemExit("Administrator already exists; bootstrap is disabled.")
            user = User(username=body.username, display_name=body.display_name)
            db.add(user)
            await db.flush()
            db.add(UserRole(user_id=user.id, role="ADMIN"))
            db.add(
                LocalCredential(
                    user_id=user.id,
                    password_hash=PASSWORD_HASH.hash(body.password.get_secret_value()),
                )
            )
            db.add(
                AuditEvent(
                    actor_id=user.id,
                    action="admin.bootstrapped",
                    target_id=user.id,
                    request_id=str(uuid.uuid4()),
                )
            )
        print("Administrator created. Sign in through the web interface.")
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["bootstrap-admin"])
    parser.parse_args()
    username = input("Admin login: ").strip()
    display_name = input("Display name: ").strip()
    password = getpass.getpass("Password (12+ characters): ")
    if password != getpass.getpass("Repeat password: "):
        raise SystemExit("Passwords do not match.")
    asyncio.run(
        bootstrap(Register(username=username, display_name=display_name, password=password))
    )


if __name__ == "__main__":
    main()
