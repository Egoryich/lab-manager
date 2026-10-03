import asyncio

from alembic import context
from lab_manager import (
    catalog_models,  # noqa: F401
    network_models,  # noqa: F401
    nodes,  # noqa: F401
    operation_models,  # noqa: F401
    reservation_models,  # noqa: F401
    runtime_models,  # noqa: F401
)
from lab_manager.config import Settings
from lab_manager.models import Base
from sqlalchemy.ext.asyncio import create_async_engine


def configure(connection):
    context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def online():
    url = (
        context.config.attributes.get("database_url") or Settings().database_url.get_secret_value()
    )
    engine = create_async_engine(url, connect_args={"connect_timeout": 5})
    async with engine.connect() as connection:
        await connection.run_sync(configure)
    await engine.dispose()


if context.is_offline_mode():
    context.configure(
        url=Settings().database_url.get_secret_value(),
        target_metadata=Base.metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(online())
