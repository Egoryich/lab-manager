# Среда разработки и эксплуатационный baseline

Обновление 2026-09-20: добавлена запускаемая вертикаль identity/groups, lockfiles, миграция, dev Compose и CI-конфигурация. Инструкция: [local-start.md](local-start.md); точные границы и выполненные проверки: [implementation-status.md](implementation-status.md). D1/D2 целиком ещё не завершены. Наблюдения ниже от 2026-09-16 сохранены как история подготовки.

## Локально проверено 2026-09-16

| Инструмент | Наблюдение |
|---|---|
| Node.js | v24.16.0, доступен |
| npm | 11.13.0, доступен через npm.cmd |
| uv | 0.9.8; каталог managed Python недоступен текущей сессии |
| Python | `python` не найден в PATH; установленность 3.13+ не подтверждена |
| Docker CLI | 28.5.1; docker engine pipe отсутствует, server version не получена |
| Git | Пустой репозиторий, существующих коммитов до подготовки нет |

Это факты текущей сессии, не заключение о всём компьютере. Docker Desktop и системные службы не запускались. Права к пользовательским каталогам не менялись. Для проверки документов достаточно доступного Node:

```sh
node tools/validate-preparation.mjs
```

## Воспроизводимая разработка D1

Предлагаются uv для Python workspace и npm workspaces для frontend/shared-types/client-sdk. Фиксировать конкретные совместимые версии resolver-ом, коммитить lockfiles, сборку повторять с frozen/locked install. Python минимум 3.13 по ТЗ; exact patch выбирается после проверки зависимостей. Для custom Guacamole extension потребуются JDK/build tool версии, совместимой с выбранным релизом Guacamole; этот дополнительный build runtime не заменяет Python backend.

Dev Compose предоставляет настоящий PostgreSQL и Redis с volumes, health checks и портами только на localhost. Приложения могут запускаться на host или отдельным Compose profile. Agent и сетевые интеграции проверяются в Linux стенде; Windows локально не эмулирует Proxmox bridge/firewall.

Production Compose отдельно содержит reverse proxy, API, worker/reconciler, PostgreSQL, Redis и observability по ресурсам VPS. PostgreSQL/Redis не публикуются в Интернет. Proxmox и агент не контейнеризуются на VPS вместо физического node. Guacamole/guacd размещаются на node в выделенном инфраструктурном госте с autostart до student workloads.

Secrets — внешние файлы/secret store с ограниченными правами, вне Git. Compose secrets позволяют выдать файлы конкретному сервису; сами исходные файлы, backup и права host остаются ответственностью развёртывания. [Docker Compose secrets](https://docs.docker.com/compose/how-tos/use-secrets/).

## Предлагаемые CI gates

Два отдельных критерия подтверждают требования к стеку и составу репозитория; они не заменяются тестами LXC/QEMU:

- **BUILD-01 (NOT_RUN), ТЗ §71:** на чистом runner подтверждены Python 3.13+, предусмотренные backend/frontend зависимости, PostgreSQL/Redis, сборка API/worker/web/agent и выбранного Guacamole extension; versions и lockfiles зафиксированы, повторный locked install/build проходит.
- **BUILD-02 (NOT_RUN), ТЗ §72:** присутствуют исполняемые приложения и все каталоги ТЗ, Alembic migrations, сгенерированные packages, тесты, инфраструктурные шаблоны и рабочий `docker-compose.yml`; проверены разрешение Compose-конфигурации и documented developer startup. Нынешние README-каталоги закрывают только подготовку структуры.

Document validator проверяет структуру подготовки и ссылки; его PASS не является прохождением BUILD-01/BUILD-02.

| Уровень | Что проверять |
|---|---|
| Документы | spec hash, requirement coverage, локальные ссылки, отсутствие неизвестных статусных claims |
| Python | Ruff/type checking по принятому baseline, domain tests, serialization и state transitions |
| Frontend | TypeScript, lint, production build, permission/role UI tests без доверия к UI как защите |
| DB | PostgreSQL migrations upgrade и constraints, transactions/races/deadlocks/retries, outbox |
| Контракты | OpenAPI diff, generated client без рассинхронизации, versioned agent protocol compatibility |
| Supply chain | Lockfiles, image versions/digests, vulnerability review и SBOM для релиза |
| Стенд | Proxmox/Guacamole/network/power tests только на явном test inventory, отчёты с версиями и временем |

Опасные стендовые тесты требуют allowlist test node/environment и ownership markers; production inventory не используется автоматически. Никакой fixture не удаляет «все VM» или все volumes. Cleanup тестов следует тем же явным IDs.

## Эксплуатационные условия для релиза

- DNS/TLS для публичного HTTPS, отдельный доступ админа VPS; WireGuard endpoint ограничен инфраструктурой.
- Backup Lab Manager отключён DEC-17: нет автоматических DB/guest export jobs. Оператор может отдельно настроить Proxmox backup. Потеря DB/keys VPS не покрывается сохранёнными guest disks; автоматическое аварийное восстановление без копии не обещается.
- Guest archive/backup и control-plane backup — разные объекты. Backup на том же физическом диске не покрывает потерю сервера.
- Настроить alert destination и retention. Backup/RPO/RTO не выдаются за обеспеченные в текущей поставке. Не отправлять уведомления третьим лицам без настройки администратором.
- Метрики: CPU/RAM/swap, storage bytes и thin metadata, disk latency, network, температуры если доступны, tunnel/agent/proxmox/guacamole, активные jobs/runs/sessions, queue age, inventory age, резервирование и quota denials.
- Alerts: warning/critical storage, неожиданно offline node/agent, gateway unavailable, failed provisioning/start, power timeout, memory pressure; dedup, acknowledge и resolved state.
- Readiness приложения проверяет зависимости; liveness не перезапускает сервис бесконечно из-за планово выключенного node. OFFLINE node — нормальное состояние продукта.
- Регламент upgrades: миграции проверены на тестовой DB, compatibility protocol поддерживается, node DRAINING, отсутствие live задач и проверка resume-state compatibility перед обновлением Proxmox/агента/Guacamole. При наличии operator backup его проверяют отдельно.

## Инфраструктурная анкета

Заполнить копию [infrastructure.example.json](infrastructure.example.json) как `infrastructure.local.json` (исключена из Git). Только характеристики и идентификаторы сетей, без паролей, приватных ключей или токенов. Null означает «неизвестно», а не готовое значение для deployment.

## Установка с нуля

Текущий путь — [deployment-from-zero.md](deployment-from-zero.md). BUILD-03 (NOT_RUN): на чистой поддерживаемой VPS и чистом Proxmox node воспроизведены документированные шаги, включая DNS/TLS, WireGuard, Tuya, Agent identity, обязательный gateway, bootstrap Admin, profiles и первый browser lesson. До появления проверенных release artifacts это будущий gate, не готовый installer.
