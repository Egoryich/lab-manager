# Database migrations

Alembic, только PostgreSQL. `0001_identity_groups` создаёт identity, сессии, восстановление, нормализованный override, группы, коды, участников и audit. В FK нет каскадного удаления guest-данных. `uv run alembic upgrade head`, затем `uv run alembic check` из корня.

Integration/E2E применяют эту же миграцию в отдельных временных схемах. Downgrade удаляет таблицы этого этапа и данные: это откат схемы, не инструмент очистки приложения.

[План реализации](../docs/implementation-plan.md).
