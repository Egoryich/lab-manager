# Lab Manager

Система управления учебной лабораторией на Proxmox VE, в разработке.

**Реализованы учётные записи и группы, каталог профилей, назначение политик и подготовка окружений с расчётом ресурсов.**
FastAPI + PostgreSQL/Redis, React-интерфейс, миграция Alembic, OpenAPI/TypeScript-контракт, lockfiles и тесты. Управление Proxmox, машинами, сетями, ресурсами и Guacamole ещё не реализовано; подключения к вашим VPS/Proxmox не выполнялись.

**Запуск на своём компьютере:** [пошаговая инструкция](docs/local-start.md). Точный состав, проверки и ограничения: [статус реализации](docs/implementation-status.md).

**Развёртывание с нуля:** [единый гайд без адресов инсталляции и секретов](docs/deployment-guide.md), включая обезличенную запись настройки Proxmox/Headscale, Docker, первый запуск и автообновление.

**Ветки:** `main` — проверенное совместимое состояние; `dev-vps` — VPS; `dev-proxmox` — физический сервер. [Правила разработки и выпуска](docs/git-workflow.md). [Pull-обновление VPS](docs/vps-auto-update.md) устанавливается отдельным systemd timer; выпуски с миграциями или новым Compose требуют ручного развёртывания.

**Первый VPS-запуск:** [контейнеры и существующий Caddy](docs/vps-first-install.md). GitHub собирает и проверяет образы `dev-vps`; установка выполняется вручную по успешному SHA. Это поставка реализованной части, без запуска учебных машин.

Получены характеристики физического сервера и VPS: [оценка ограничений](docs/hardware-assessment.md). Подготовка окружения пока не резервирует ресурсы и не создаёт машины на Proxmox.

## Основное задание

[LAB_MANAGER_CODEX_SPEC.md](LAB_MANAGER_CODEX_SPEC.md) — неизменённая копия предоставленного ТЗ версии 2.0, все 76 разделов. Исходный файл назывался `LAB_MANAGER_CODEX_SPEC (1).md`.
Сведения о происхождении и SHA-256: [spec-source.json](docs/spec-source.json).

Исходное ТЗ сохранено для истории. [Уточнения пользователя](docs/decisions.md) имеют приоритет и явно меняют lifecycle, удаление участников, приватность Teacher и backup/snapshots. Остальные технические предложения обозначены отдельно.

## Начать здесь

1. [Принятые решения пользователя](docs/decisions.md), [готовность и оставшиеся данные](docs/readiness.md).
2. [Архитектура](docs/architecture.md), [модели и ограничения](docs/domain.md), [состояния и операции](docs/lifecycle.md).
3. [Права](docs/permissions.md), [план API](docs/api.md), [сеть](docs/networking.md), [Guacamole](docs/guacamole.md), [безопасность](docs/security.md).
4. [Резервирование ресурсов](docs/scheduling.md), [диски, архивы и очистка](docs/storage.md).
5. [Этапы разработки](docs/implementation-plan.md), [приёмка](docs/acceptance.md), [покрытие ТЗ](docs/requirements.json).
6. [Среда разработки и эксплуатация](docs/development.md), [анкета инфраструктуры](docs/infrastructure.example.json).
7. [Развёртывание с нуля](docs/deployment-from-zero.md), [Tuya](docs/power-tuya.md), [календарь](docs/bookings.md).
8. [Итог проверки подготовки](docs/preparation-report.md).

## Структура

```text
apps/api/                 FastAPI, домен, orchestration и worker
apps/web/                 React, TypeScript, Vite
apps/node-agent/          Типизированный агент, systemd, адаптер Proxmox
packages/shared-types/   Генерируемые модели публичного API
packages/client-sdk/     Генерируемый клиент API
infra/                   Docker, WireGuard, Guacamole, systemd, Ansible
migrations/              Alembic, PostgreSQL
tests/                   Unit, integration, e2e, load
docs/                    Проектные решения, контракты и критерии готовности
tools/                   Проверки подготовительных материалов
```

Исполняемый код сейчас находится в API, web, миграциях, пакетах контрактов и тестах. Остальные компоненты содержат проектное описание и будут реализованы следующими этапами. Dev Compose запускает PostgreSQL и Redis; production-развёртывание полного продукта ещё не готово.

## Проверка подготовительных материалов

```sh
node tools/validate-preparation.mjs
```

Проверка контролирует целостность ТЗ, наличие всех 76 разделов в матрице, ссылки между документами и состав репозитория. Она не проверяет работу будущего продукта.

## Неизменные свойства продукта

- VPS — постоянно доступный control plane; node может быть выключен.
- Group и Environment постоянны; EnvironmentRun отражает отдельный запуск.
- Один Runtime на студента в окружении; конец пары: VM hibernate, LXC shutdown, диски сохраняются. Kick удаляет данные Student только в выбранной Group.
- Доступ к машинам, предоставляемый системой, проходит только через браузер и Apache Guacamole.
- Авторизация и квоты проверяются backend; PostgreSQL обеспечивает транзакционное резервирование.
- Сети студентов изолированы от management; ISOLATED и GROUP_LAN обязательны.
- Потеря связи, нехватка места и обнаружение orphan не разрешают уничтожать данные.

У Teacher только свои объекты, обязательная Demo и возможность помогать собственному Student через Guacamole. Архив остаётся на Proxmox; backup выключен, snapshots только Admin. Для нового template создаётся новое Environment.
