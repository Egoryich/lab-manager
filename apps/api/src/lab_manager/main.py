import asyncio
import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from lab_manager import (
    auth,
    catalog,
    groups,
    lesson_booking,
    lesson_preview,
    node_policies,
    nodes,
    operations,
)
from lab_manager.config import Settings
from lab_manager.dependencies import Problem
from lab_manager.schema import CURRENT_SCHEMA_REVISION
from lab_manager.schemas import ErrorView
from lab_manager.security import SecretCodec

logger = logging.getLogger("lab_manager")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app):
        yield
        await app.state.redis.aclose()
        await app.state.engine.dispose()

    app = FastAPI(
        title="Lab Manager",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None if settings.environment == "production" else "/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.settings = settings
    app.state.password_slots = asyncio.Semaphore(settings.password_hash_concurrency)
    app.state.codec = SecretCodec(
        settings.digest_key.get_secret_value(), settings.encryption_key.get_secret_value()
    )
    app.state.engine = create_async_engine(
        settings.database_url.get_secret_value(),
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=0,
        hide_parameters=True,
        connect_args={"connect_timeout": 5},
    )
    app.state.sessions = async_sessionmaker(app.state.engine, expire_on_commit=False)
    app.state.redis = Redis.from_url(
        settings.redis_url.get_secret_value(), socket_connect_timeout=3, socket_timeout=3
    )

    @app.middleware("http")
    async def security_boundary(request: Request, call_next):
        request.state.request_id = str(uuid.uuid4())
        if request.method not in {"GET", "HEAD", "OPTIONS"} and (
            request.headers.get("origin") != settings.public_origin
            or request.headers.get("sec-fetch-site") == "cross-site"
        ):
            response = JSONResponse(
                status_code=403,
                content={
                    "code": "ORIGIN_DENIED",
                    "message": "Источник запроса не разрешён.",
                    "request_id": request.state.request_id,
                },
            )
        else:
            response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.exception_handler(Problem)
    async def problem_handler(request, exc):
        return JSONResponse(
            status_code=exc.status,
            content={
                "code": exc.code,
                "message": exc.message,
                "request_id": request.state.request_id,
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request, exc):
        # Pydantic errors can include password/token input. Never return or log that input.
        return JSONResponse(
            status_code=422,
            content={
                "code": "VALIDATION_FAILED",
                "message": "Проверьте введённые данные.",
                "request_id": request.state.request_id,
            },
        )

    @app.exception_handler(SQLAlchemyError)
    async def database_handler(request, exc):
        logger.error(
            "database_failure request_id=%s type=%s", request.state.request_id, type(exc).__name__
        )
        return JSONResponse(
            status_code=503,
            content={
                "code": "DATABASE_UNAVAILABLE",
                "message": "Сервис временно недоступен.",
                "request_id": request.state.request_id,
            },
        )

    @app.get("/api/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/health/ready")
    async def ready() -> dict[str, str]:
        try:
            async with app.state.engine.connect() as connection:
                revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
                if revision != CURRENT_SCHEMA_REVISION:
                    raise Problem(503, "MIGRATION_REQUIRED", "Требуется обновить схему базы.")
            await app.state.redis.ping()
        except (SQLAlchemyError, RedisError) as error:
            raise Problem(503, "DEPENDENCY_UNAVAILABLE", "Сервис временно недоступен.") from error
        return {"status": "ready"}

    errors = {code: {"model": ErrorView} for code in (400, 401, 403, 404, 409, 422, 429, 503)}
    app.include_router(auth.router, prefix="/api", responses=errors)
    app.include_router(groups.router, prefix="/api", responses=errors)
    app.include_router(catalog.router, prefix="/api", responses=errors)
    app.include_router(operations.router, prefix="/api", responses=errors)
    app.include_router(nodes.router, prefix="/api", responses=errors)
    app.include_router(node_policies.router, prefix="/api", responses=errors)
    app.include_router(lesson_preview.router, prefix="/api", responses=errors)
    app.include_router(lesson_booking.router, prefix="/api", responses=errors)
    return app
