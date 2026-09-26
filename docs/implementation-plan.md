# План реализации полного продукта

Объём поставки определяется исходным ТЗ и [decisions.md](decisions.md); backup/export и co-teacher исключены новыми решениями. Остальные этапы входят в поставку. Это порядок зависимостей, а не сокращение объёма до демонстрационного MVP. Выполнен D0; 2026-09-20 начаты D1/D2 и реализована первая вертикаль identity/groups. Подробности и доказательства: [implementation-status.md](implementation-status.md).

| Этап | Результат | Проверка выхода |
|---|---|---|
| D0. Подготовка | ТЗ в репозитории, анализ пробелов, целевая архитектура, домен, API plan, права, матрица требований, структура | Все 76 разделов учтены, вопросы отделены от принятых требований; выполнен document validator |
| D1. Основа и проверка интеграций | Поддерживаемые версии/lockfiles, dev PostgreSQL/Redis, packages, migrations, logging, CI; мини-проверки Guacamole, network и PowerProvider | Воспроизводимый install/build, реальный PostgreSQL; отчёты о token/revoke/session tracking, host isolation и power через независимый канал |
| D2. Identity, permissions, группы | Local signup + Admin recovery, users/roles, policies/overrides, profiles visibility, membership/code/ban, destructive kick, Admin transfer, audit | API/IDOR tests; 30 joins; нормализованные права; запрещённый профиль недоступен и через API |
| D3. Состояния и учёт | Environment/config/template versions, Run/Runtime/Demo, durable operations, outbox, agent protocol, node inventory, transactional ledgers и календарь | PostgreSQL race tests teacher/node/storage; stop сохраняет disk commitment; crash/replay не удваивает эффекты |
| D4. Proxmox и сети | LXC/QEMU create/start/stop, IPAM/VMID, готовые images, ISOLATED/GROUP_LAN, node power/drain | На стенде root guest не достигает management/соседнего isolated; две группы изолированы; shutdown guards выдерживают race |
| D5. Доступ и занятия | Guacamole extension, teacher assistance, browser flow, credentials reveal, session revoke, предупреждения, idle, VM hibernate/LXC shutdown, sync members, обязательная demo | 30 сессий + demo; собственный runtime only; stop/start той же машины; удаление membership обрывает доступ |
| D6. Storage и восстановление | Pool telemetry, thin data/metadata, quotas, Admin snapshots, SOFT_ARCHIVE/unarchive, hibernate states, previews, cleanup/dependencies, orphans | Пороговые сценарии; возврат тех же машин после unarchive; невозможно случайно удалить активный/чужой/зависимый объект |
| D7. Полный UI и эксплуатация | Все student/teacher/admin страницы, realtime, history/audit, alerts, settings, отключённый backup, Ansible/systemd/Compose и установка с нуля | Все сценарии приёмки; accessible UI, ошибки и offline states; проверенная чистая установка VPS/node/gateway |
| D8. Системная приёмка | Матрица требований закрыта доказательствами, security/load/fault reports, runbooks и релиз | Реальный целевой сервер, 30 студентов + demo, несколько преподавателей, нагрузка с ограниченным storage, reboot/recovery |

UI разрабатывается вместе с вертикальными сценариями D2–D6; D7 доводит весь продукт, а не начинает интерфейс с нуля. Наличие всех таблиц без работающих workflows не означает завершение этапа.

## Первые задачи D1

1. Реализовывать принятые DEC-01…17: local signup, soft archive, VM hibernate/LXC shutdown, обязательная Demo, private calendars, destructive kick, backup disabled и Admin-only snapshots. Получить hardware inventory и реальный Tuya status mapping.
2. Проверить Python 3.13+ и Node toolchain, доступ к PostgreSQL/Redis в Linux/Docker среде. Выбрать поддерживаемые версии и создавать lockfiles только реальным resolver.
3. Создать Python workspace API/agent, frontend workspace, правила импортов и CI. Production-конфигурация не использует SQLite, memory queue или фальшивый access provider.
4. Оформить полный OpenAPI для первой вертикали, DTO errors/operations и generated TS types. Статусы enum и serialization contract проверять автоматически.
5. Реализовать durable operation schema/outbox и unit-of-work до внешних create/delete команд. Проверить рестарт между SQL commit и provider call.
6. Выполнить три коротких инженерных исследования на согласованном стенде: Guacamole авторизация/отзыв; Proxmox host isolation; независимый power-on. Их код становится тестами/адаптерами, а не отдельной временной платформой.

## Оставшиеся зависимости

- Hardware inventory блокирует конкретные storage/сетевые настройки и подтверждение вместимости, не домен.
- Tuya status mapping и повторяемость Power проверяются на реальной плате; Cloud transport и commands уже определены.
- Guacamole shared tunnel и VM hibernate проверяются до объявления поддержки соответствующих профилей.
- Длительности брони/idle/boot margin — настраиваемые значения Admin после измерений.
- Backup/export и co-teacher не являются отложенными блокерами текущего релиза: пользователь исключил эти функции.

## Definition of done для задачи

Требование связано с тестируемым сценарием; разрешение проверяется на сервере; схема имеет инварианты и migration; повторы/ошибки/отмена объяснимы; audit не содержит secrets; публичный contract обновлён вместе с SDK; meaningful tests выполнены; документировано, какие проверки требуют стенд и не запускались.

## Definition of done для продукта

Все действующие критерии §74 и сценарии §75 с изменениями DEC-01…17 подтверждены, обязательные load/network tests §§64–65 выполнены на согласованной конфигурации. Нет открытых критических решений по питанию, identity, данным и сетевой защите. Проверены отключение backup endpoints/jobs, reconnect, частичный provisioning, offline node, запрет автопотери данных, безопасная очистка и передача ownership/quotas. Администратор получает инструкции эксплуатации и восстановления.

Не принимать «работает fake provider», «30 async HTTP запросов» или «успешный docker build» за доказательство 30 работающих гостевых систем и браузерных сессий. Целевые длительности запуска, RPO/RTO и допустимые задержки согласуются по измерениям и требованиям пользователя, а не выдумываются.
