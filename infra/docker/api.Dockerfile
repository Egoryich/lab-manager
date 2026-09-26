FROM ghcr.io/astral-sh/uv:0.9.8 AS uv
FROM python:3.13-slim-bookworm
COPY --from=uv /uv /uvx /bin/
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY apps/api apps/api
RUN uv sync --frozen --no-dev --no-editable
COPY alembic.ini ./
COPY migrations migrations
RUN useradd --system --uid 10001 --no-create-home lab
USER 10001
EXPOSE 8000
CMD ["/app/.venv/bin/uvicorn", "lab_manager.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers", "--no-access-log"]
