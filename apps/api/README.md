# Backend API и worker

FastAPI API находится в `src/lab_manager`: настройки, PostgreSQL-модели, серверная аутентификация, группы, аудит, bootstrap CLI. Durable operations, worker и scheduler ещё не реализованы. PostgreSQL/Redis обязательны; SQLite и фиктивных provider endpoints нет.

Запуск: `uv run python -m lab_manager` из корня. [Полная локальная инструкция](../../docs/local-start.md).

[План реализации](../../docs/implementation-plan.md).
