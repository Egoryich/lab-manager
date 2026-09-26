# План публичных и внутренних контрактов

ТЗ §§56–62 задаёт обязательный минимум. Ниже — проект полного API, ещё не OpenAPI контракт работающего приложения. Изменения пользователя описаны в [decisions.md](decisions.md); при реализации Pydantic/OpenAPI становятся единственным источником генерируемых SDK.

## Общие правила

- Публичный namespace сохраняет `/api`. Поля UUID, время UTC ISO 8601, ресурсы с явными единицами, cursor pagination и bounded limits.
- Сессия пользователя — защищённая HttpOnly/Secure cookie; CSRF защита изменяющих операций, проверка Origin, ограниченный CORS. Локальные login/register; self-registration только Student, recovery только через Admin.
- Проверки: active user → роль → membership/ownership/delegation → effective permission → state → quota. Student ID из тела не заменяет текущего actor.
- Длительные mutations возвращают `202 Accepted`, `operation_id` и `Location: /api/operations/{id}`. HTTP success означает принятие, не готовность машины.
- `Idempotency-Key` обязателен для start/stop/provision/power/snapshot/reset/archive/delete/cleanup; ключ ограничен actor+route+target и хранится с digest тела. Повтор с другим телом — 409; с тем же — исходная операция. Истечение HTTP-key TTL не отменяет domain uniqueness/agent dedup.
- PATCH/переносы и планы разрушения используют `If-Match` или явный `expected_version`. Изменившаяся версия — 409/412; silent last-write-wins запрещён.
- Ошибка содержит `code`, безопасное `message`, `request_id`, `details` для UI. Коды: 401, 403, 404 для недоступного чужого объекта, 409 state/quota conflict, 422 validation, 429 rate limit, 503 provider unavailable. Traceback и инфраструктурные credentials не возвращаются студенту. Внутренний IP собственной машины разрешён; пароль выдаётся отдельным scoped reveal.
- DELETE с сохранёнными зависимостями не каскадирует молча. Конкретный destructive plan передаётся заголовком `X-Destructive-Plan` или typed body POST-команды; transport выбирается единообразно до фиксации OpenAPI. Plan содержит actor, IDs, versions, fingerprint, expiry и подтверждение пользователя.

## Endpoint minimum из ТЗ

| Endpoint | Авторизация и семантика |
|---|---|
| POST /api/groups | Teacher `can_create_groups`; owner=current teacher, создание кода |
| GET /api/groups | Scope текущего пользователя, у Admin общий поиск |
| GET /api/groups/{id} | Своя группа/ACTIVE member; student DTO без join code и служебных полей |
| PATCH /api/groups/{id} | Owner/Admin; permitted fields, version |
| DELETE /api/groups/{id} | `can_delete_groups` + owner/Admin; блокируется при неудалённых Environment, без каскада данных |
| POST /api/groups/{id}/join-code/regenerate | Управление roster; аудит и немедленный revoke старого кода |
| POST /api/groups/{id}/join-code/enable | Управление roster; группа не archived |
| POST /api/groups/{id}/join-code/disable | Управление roster; существующие membership остаются |
| POST /api/groups/join | Authenticated Student; `{code, confirm:true}`, rate limit, запрет BANNED, идемпотентное членство |
| GET /api/groups/{id}/members | Teacher/Admin; student не получает общий каталог PII автоматически |
| DELETE /api/groups/{id}/members/{student_id} | DEC-11: destructive plan по Student в этой Group → revoke → stop/delete всех его Runtime, snapshots, RAM states, включая архивные; другие группы/аккаунт не затрагиваются |
| POST /api/environments | Teacher group scope + profile permission; конфигурация и template version закрепляются |
| GET /api/environments | Фильтры с backend scope; студент видит свои доступные окружения |
| GET /api/environments/{id} | Разные student/teacher/admin projections |
| PATCH /api/environments/{id} | Owner/Admin + version; template/kind in-place запрещены, для другого template новое Environment; metadata и разрешённые настройки только при совместимом state |
| POST /api/environments/{id}/start | Teacher/Admin; текущее право на kind/profile/network; power право отдельно при необходимости boot |
| POST /api/environments/{id}/stop | Завершить пару: предупреждение по policy → VM hibernate, LXC shutdown → release compute после подтверждения; без удаления disks |
| POST /api/environments/{id}/sync-members | Teacher/Admin; только недостающие ACTIVE members; delta reservation |
| POST /api/environments/{id}/snapshot | Только Admin по DEC-17; quota/capability/state checks; Teacher/Student получают 403 независимо от старых permission overrides |
| DELETE /api/environments/{id} | `can_delete_own_environments`/Admin, destructive plan, завершённый stop |
| GET /api/environments/{id}/runtimes | Student — только собственный; Teacher/Admin — разрешённый scope без секретов |
| POST /api/runtimes/{id}/start | Student только свой idle-stopped runtime активного run по policy; Teacher/Admin в scope |
| POST /api/runtimes/{id}/shutdown | Teacher/Admin, либо отдельное разрешение self-service; сохраняет disk |
| POST /api/runtimes/{id}/restart | Teacher/Admin; self-service не предполагается из владения |
| POST /api/runtimes/{id}/snapshot | Только Admin по DEC-17; quota/capability/state checks; Teacher/Student получают 403 независимо от старых permission overrides |
| POST /api/runtimes/{id}/reset | Teacher/Admin, plan с потерей данных; snapshot только если capability/space позволяет |
| POST /api/runtimes/{id}/open | Student — свой Runtime; Teacher — Runtime своего Environment; browser-only flow и актуальный run/booking. Shared assistance использует отдельное разрешение на активный tunnel |
| GET /api/environments/{id}/demo | Teacher с соответствующим scope, не Student |
| POST /api/environments/{id}/demo/start | Demo profile grant + активный run, budget включён; без старта Environment в обход policy |
| POST /api/environments/{id}/demo/stop | Управляющий Teacher/Admin; persistent disk |
| POST /api/environments/{id}/demo/open | Разрешённый Teacher; Guacamole only |
| POST /api/environments/{id}/demo/snapshot | Только Admin по DEC-17; quota/capability/state checks; Teacher/Student получают 403 независимо от старых permission overrides |
| POST /api/environments/{id}/demo/reset | Destructive plan и отдельный audit |
| GET /api/storage | Admin; Teacher получает отдельную проекцию своей квоты |
| GET /api/storage/pools | Admin; capability, free, committed, telemetry age |
| GET /api/storage/usage | Admin; breakdown, unknown usage и измерительная погрешность |
| GET /api/storage/candidates | Admin; candidates не являются разрешениями на удаление |
| POST /api/storage/candidates/{id}/delete | Admin, plan, exact ID, актуальные dependency checks |
| POST /api/storage/candidates/{id}/archive | Admin: только логический SOFT_ARCHIVE принадлежащего Environment; standalone disk export/backup отключён, неподдержанная цель отвергается |
| POST /api/storage/cleanup | Admin, bounded explicit IDs + plan; никакого wildcard/all |
| GET /api/admin/teacher-permission-policies | Admin |
| POST /api/admin/teacher-permission-policies | Admin, нормализованные permissions/limits |
| PATCH /api/admin/teacher-permission-policies/{id} | Admin + expected revision; invalidation и аудит |
| PUT /api/admin/teachers/{id}/permission-policy | Admin; атомарная замена assignment |
| PATCH /api/admin/teachers/{id}/permissions | Admin; explicit allow/deny/clear inheritance, typed limit overrides |

## Обязательные дополнения контракта

Эти routes — проектные названия для функций, уже необходимых полному продукту; перед реализацией фиксируются в OpenAPI.

| Функция | Предлагаемые routes / требования |
|---|---|
| Identity | GET /api/me; POST /api/auth/register, /login, /logout; POST /api/admin/users/{id}/password-reset и POST /api/auth/password-reset/complete; только Admin выдаёт одноразовое reset-разрешение |
| Эффективные права | `GET /api/me/permissions`, Admin user/teacher/student lifecycle, suspend и revoke |
| Управление Group | POST /api/groups/{id}/transfer только Admin; POST .../archive и .../unarchive. Co-teacher/delegation routes в текущей поставке отсутствуют |
| Membership ban | `POST /api/groups/{id}/members/{student_id}/ban`, `.../unban`; данные гостя не удаляются |
| Владение окружением | `POST /api/environments/{id}/transfer` с квотным планом и переносом billing owner |
| Preflight и история | `POST /api/environments/{id}/preflight`, `GET .../runs`; preflight не является reservation |
| Архив | POST /api/environments/{id}/archive и .../restore выполняют SOFT_ARCHIVE/unarchive; те же Runtime/диски/owners, без export и освобождения storage |
| Destructive previews | `POST /api/destructive-plans` с action, explicit IDs, expected versions; execute потребляет plan один раз |
| Operations | `GET /api/operations/{id}`, `POST .../cancel`, `.../retry`; только по доступному target, состояние ошибки без секретов |
| Nodes/power | `GET /api/nodes`, `GET /api/nodes/{id}`, `GET .../capacity`, `POST .../power-on`, `POST .../shutdown`; отдельный force endpoint только Admin |
| Catalog | Teacher-filtered `GET /api/profiles`; Admin CRUD profiles/templates/versions, enable/retire, capabilities |
| Network | Admin versioned policies, Teacher PATCH environment через policy проверки; runtime no arbitrary bridge/IP |
| Snapshots | Только Admin: GET /api/runtimes/{id}/snapshots, POST /api/snapshots/{id}/restore, DELETE /api/snapshots/{id} с plan. Teacher endpoints недоступны |
| Обновление template | Upgrade существующего Environment исключён DEC-14; POST /api/environments создаёт новое, прежнее сохраняется до отдельного удаления |
| Дополнительные действия Demo | `POST /api/environments/{id}/demo/restart`, `POST .../demo/credentials/reveal`; те же проверки владения, ресурсов и секрета, что у соответствующих runtime actions |
| Guest credentials | `POST /api/runtimes/{id}/credentials/reveal` — только текущий владелец/специальное полномочие, audit, no-store, reauth policy |
| Access | `POST /api/access/grants/{id}/redeem`, `POST /api/access/sessions/{id}/revoke`; реализация Guacamole handshake подтверждается spike |
| Background protection | Bounded `POST /api/runtimes/{id}/activity-protection`, revoke/expiry; политика учителя |
| Observability | `GET /api/events` SSE, `GET /api/alerts`, acknowledge, `GET /api/admin/audit`, system health/settings |

Teacher может подключаться к машине своего Student по DEC-10; shared assistance идёт через проверенный активный Guacamole tunnel, с индикатором и аудитом. Service credentials не раскрываются. Нет доступа к чужому Environment. Demo обязательна и принадлежит владельцу Environment.

## Пример асинхронного start

```http
POST /api/environments/{environment_uuid}/start
Idempotency-Key: <client-generated-uuid>
Content-Type: application/json

{"expected_version": 7}
```

```json
{
  "operation_id": "<operation_uuid>",
  "state": "QUEUED",
  "environment_id": "<environment_uuid>",
  "status_url": "/api/operations/<operation_uuid>"
}
```

Backend capacity result содержит required/available/reserved по RAM, CPU, storage, demo, overhead, quota, freshness и понятную причину отказа. Клиент может отобразить числа, но не принимает решение локально.

## Открытие машины и события

`open` при готовой машине выдаёт короткоживущее одноразовое разрешение, связанное с user/runtime/run/session и одной целью подключения. Если требуется start/readiness — возвращает Operation; после готовности клиент повторяет авторизованное открытие. Не возвращаются внешний SSH/RDP endpoint, VPN конфигурация или service credentials. Собственный private IP может быть отображён отдельно от launch authorization.

Guacamole flow не строится на произвольном JWT, который стандартный Guacamole якобы понимает. Нужен проверенный integration extension/bridge, redemption, revocation и серверная session telemetry: [guacamole.md](guacamole.md).

SSE событие: `event_id`, `type`, `aggregate_id`, `aggregate_version`, `occurred_at`, разрешённый payload. Поддерживается reconnect cursor/Last-Event-ID, а при выпадении из retention — resync. Actor потерял membership — event subscription и access sessions отзываются.

## Внутренний Agent protocol

Отдельный versioned API по mTLS внутри WireGuard; недоступен браузеру. Envelope: `protocol_version`, `operation_id`, `step_id`, `command_id`, `node_id`, `boot_generation`, `resource_generation`, `fencing_token`, `deadline`, `typed_payload`, `request_digest`.

Операции §46 сохраняются полностью: node.inventory/health; runtime.create/start/shutdown/stop/reboot/delete/snapshot/rollback/resize; network.create/attach/delete; storage.inventory/cleanup_candidate_scan. Дополняются query task status/capabilities и подписанными telemetry acknowledgements. `runtime.stop` — force capability, не синоним graceful shutdown.

Agent не принимает URL/путь/VMID/bridge из произвольного student payload. Команда связана с зарегистрированным объектом и проверенной generation. Повторный command_id с другим digest отвергается. Статус различает accepted/in_progress/succeeded/failed/unknown, а не только HTTP 200/500.

## Новые контракты уточнённого продукта

- Calendar: GET /api/availability; POST/GET /api/bookings; GET /api/bookings/{id}; POST .../cancel и .../reschedule. Teacher видит только своё; availability без чужих IDs/имён.
- Teacher assistance: POST /api/runtimes/{id}/assist с mode=view|control, session_id и expected authorization revision. Backend проверяет Teacher ownership и ACTIVE Student membership; выдаёт scoped grant на один существующий tunnel.
- Persistence: typed agent runtime.hibernate и runtime.resume дополняют исходный allowlist; runtime.shutdown для LXC. Произвольные shell команды не добавляются.
- Backup/export routes не публикуются; deployment feature flag disabled. Admin snapshots и VM state artifacts — отдельные возможности.
- Final delete не является POST .../stop: используется существующий DELETE /api/environments/{id} с подтверждённым plan.
