# Shared types

TypeScript DTO генерируются из публичного OpenAPI. Не поддерживать вручную вторую копию domain enums и API models.

Из корня: `uv run python tools/export-openapi.py`, затем `npm run generate`. Сверка: `uv run python tools/check-contract.py`; CI дополнительно проверяет generated TypeScript diff.

[План реализации](../../docs/implementation-plan.md).
