# Docker

Корневой `docker-compose.yml` запускает локальные PostgreSQL/Redis с health checks и портами только на loopback. `api.Dockerfile` собирает API с frozen Python-зависимостями и непривилегированным пользователем. Production Compose полного продукта ещё не создан.

Сборка API: `docker build -f infra/docker/api.Dockerfile -t lab-manager-api:dev .`. Dockerfile не включает `.env`; настройки передаются извне, миграция выполняется отдельной командой до API.

[План реализации](../../docs/implementation-plan.md).
