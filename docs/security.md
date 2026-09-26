# Безопасность Lab Manager

Статус: подготовка разработки. ТЗ v2.0 — источник обязательных правил; дополнительные меры ниже являются предлагаемым проектным baseline и должны пройти интеграционные проверки. Документ не утверждает, что система уже реализована или аттестована.

## Границы доверия

| Зона | Доверие и основные ограничения |
|---|---|
| Browser | Недоверенный ввод, только прикладные HTTPS endpoints |
| Student Runtime | Недоверенная машина; студент имеет root/administrator |
| Teacher | Может действовать лишь в назначенных группах, permissions и лимитах |
| VPS API/worker | Проверяет policy, хранит операции и ledger; единственный control plane |
| Agent | Узкий исполнитель типизированных операций для конкретного node |
| Guacamole | Отдельная access-инфраструктура; не администратор Proxmox |
| Proxmox/BMC/storage | Высокопривилегированная инфраструктура, закрытая от учебных сетей |
| Admin | Управляет системой через audited workflow; аварийный доступ хранится отдельно |

Скомпрометированный Runtime не считается источником достоверных данных о своих правах, владельце, размере диска или состоянии firewall. Guest metrics дополняют host observations, но не заменяют их.

## Root внутри LXC и VM

ТЗ §15 прямо разрешает студенту видеть пароль собственной машины. Это не выдача host root: студент действует внутри своего Runtime. Общее правило скрытия инфраструктурных credentials (§76) не отменяет это конкретное разрешение.

Предложение для LXC: только unprivileged containers с host-side ограничениями, без privileged mode, bind mounts host filesystem, device passthrough и произвольных `lxc.*` параметров. Сохранить AppArmor/seccomp, не выдавать гостям host capabilities. Nesting доступен лишь через проверенный профиль; Docker Lab по ТЗ — VM.

В unprivileged LXC UID 0 гостя отображается в непривилегированный UID host; при этом контейнер использует ядро host. Это полезная граница, но не равнозначна отдельному ядру VM. Proxmox отдельно предостерегает от privileged containers для недоверенных сред. [Официальная документация контейнеров](https://github.com/proxmox/pve-docs/blob/master/pct.adoc).

Профили для изменения ядра, модулей, сложной вложенной виртуализации и более опасных системных лабораторий должны использовать QEMU с явно разрешённой policy. Решение, какие учебные задания разрешены в LXC, оформляется в template/profile review.

Guest root может увидеть свой IP, изменить пароль и удалить ключ доступа Guacamole. Не обещать скрытие гостевой конфигурации или неуязвимость guest credential к самому владельцу. При нарушении readiness показать контролируемую ошибку и сценарий восстановления; не сбрасывать данные автоматически.

## Аутентификация и прикладные сессии

Источник identity определён DEC-03: локальная самостоятельная регистрация Student и recovery через Admin. В домене использовать стабильный внутренний `user_id`; внешний subject связывать через issuer+subject. Строка имени или email не становится первичным ключом владения.

Предложение baseline:

- Серверные browser sessions с `Secure`, `HttpOnly`, подходящим `SameSite`, rotation при входе и повышении прав.
- CSRF-защита изменяющих cookie-authenticated запросов; проверка `Origin` при WebSocket upgrade.
- Отдельные ограничения частоты для login, восстановления аккаунта, group join, credential reveal и открытия Runtime.
- Обязательная усиленная аутентификация Admin и повторная проверка личности при критичных действиях; конкретный MFA механизм выбирается вместе с identity provider.
- Отзыв browser session и блокировка пользователя отзывают связанные доступы Guacamole.
- Роли/permissions не берутся из browser payload; внешние claims отображаются через серверную policy.

Ошибки join/login не должны позволять массово перечислять аккаунты и коды. Код группы проверяется только после входа, нормализуется без учёта регистра, генерируется криптографически стойко из допустимого алфавита и уникален среди активных кодов.

Предложение хранения кода: keyed hash для поиска и зашифрованное значение для показа преподавателю. Регенерация немедленно отключает старый код. Попытки ограничиваются по аккаунту и сетевому источнику; успешное вступление не раскрывает состав других групп.

## Авторизация

Каждый API/use case проверяет actor, effective permissions, ownership своего Environment, membership, object state и лимиты. Скрытие кнопки в UI не заменяет запрет backend.

Студент открывает только собственный Runtime в группе с действующим membership и разрешённым Environment state. Наличие VMID или connection ID не даёт права на объект. Teacher не получает доступ к чужим Runtime или группе из-за одинакового профиля либо нахождения на том же node.

Перед выполнением отложенной операции снова проверять актуальную policy и ownership. Job, поставленный до отзыва прав, не должен автоматически сохранять опасные полномочия. Для уже выполняемой операции определить безопасную точку остановки и записать решение в audit.

Разделять разрешение на создание QEMU и разрешение на DemoRuntime конкретного профиля. Лимиты Admin/Teacher вычисляются сервером и резервируются транзакционно; пакетное действие не обходится множеством одиночных запросов.

Изменение membership, перевод группы другому преподавателю, блокировка пользователя и начало удаления Environment создают события отзыва доступа. DEC-11 заменяет §19: исключение запускает подтверждённое удаление данных этого Student во всех Environment выбранной Group и немедленно отзывает его sessions. Другие Group не затрагиваются.

## Credentials

| Секрет | Кто может получить | Хранение/жизненный цикл |
|---|---|---|
| Пароль своей гостевой машины | Её Student; Demo credential — назначенный Teacher | Уникален на Runtime; выдача по проверенному reveal workflow |
| Guacamole connection parameters | Только доверенный access backend | Secret reference, без возврата endpoint/ключей инфраструктуры в UI |
| Proxmox API token | Agent/provider, которому необходим | Отдельная сервисная identity, минимальные ACL, rotation/revocation |
| WireGuard private key | Узел-владелец peer | Раздельный на peer, не в DB payload браузера |
| mTLS key Agent | Конкретный node | Индивидуальная identity, срок действия, отзыв |
| BMC/provider credential | PowerProvider executor | Отдельно от runtime control credentials |
| Ключи шифрования и backup keys | Секретное хранилище/оператор восстановления | Вне репозитория и вне самих DB backups |

Предлагаемый endpoint `POST /api/runtimes/{id}/credentials/reveal`: только владелец с актуальной авторизацией, при необходимости повторный вход, `Cache-Control: no-store`. Выдавать уникальный гостевой пароль по явному действию и регистрировать `runtime.credentials.reveal` без содержимого секрета. Аналогичный demo endpoint ограничен назначенным Teacher. Политика доступа Admin к гостевым паролям требует отдельного явного permission.

Пароль скрыт до запроса; он отсутствует в URL, analytics, exception trace, audit payload и логах reverse proxy. Его значение не включается в обычные Runtime DTO или SSE. Поля модели содержат ссылки на зашифрованные секреты и версию, а не общий student password.

Шифрование at rest предусматривает отдельный ключ и rotation. Конкретное secret store определяется deployment ADR. Компрометация DB вместе с доступным приложению ключом остаётся отдельным сценарием угрозы; наличие шифрования не разрешает экспортировать дамп без защиты.

Если студент сам изменил гостевой пароль, сервер не знает нового значения автоматически. UI должен показывать состояние актуальности credential; восстановление доступа через утверждённую операцию не должно молча перезаписывать работу студента.

## Минимальные права Agent и Proxmox

Agent принимает только операции из allowlist ТЗ §46 с типизированной схемой, ограничением размеров, deadline, `operation_id`, `node_id`, desired revision и fencing generation. Нет `shell`, `command`, arbitrary URL, пути host filesystem или исполняемого скрипта из API.

Аутентификация: предложение mTLS поверх WireGuard, привязка сертификата к node identity, отдельная CA/trust policy. Нахождение внутри VPN не заменяет авторизацию. Просроченный/отозванный сертификат и несовпадение node_id отклоняются.

Основной Python-процесс работает отдельным системным пользователем. Обычные VM/LXC операции выполняются через Proxmox API token. Для сетевых действий, которым нужны host privileges, отдельный локальный helper принимает узкие структуры и проверяет допустимые сегменты/интерфейсы. Он не выполняет переданную строку shell, не предоставляет универсальный sudo и не меняет management сеть по запросу teacher.

Proxmox поддерживает API token с separated privileges: эффективные права ограничены одновременно ACL пользователя и токена. Выдавать token сервисному пользователю, не `root@pam`. [Официальная модель API tokens](https://github.com/proxmox/pve-docs/blob/master/pveum.adoc).

Перед реализацией составить и проверить endpoint-to-privilege матрицу на закреплённой версии: inventory, clone/create, power, disk, snapshot, network, cleanup. Ограничить VM/resource pool, storage и SDN пути там, где API это позволяет. `VM.Allocate` и storage-права требуют отдельной проверки области действия; нельзя называть один универсальный токен «минимальным» без negative tests.

Системному identity не нужны выдача прав пользователям, Proxmox Console и полный shell host. Особенно опасные network/storage privileges отделяются от штатного runtime power management. Если API требует более широкую привилегию, это фиксируется в threat model, а компенсирующая проверка object ownership выполняется в helper/agent.

Перед изменением VMID, диска или snapshot Agent сопоставляет opaque domain ID, provider ID и метаданные владения. Переданный числовой ID сам по себе не разрешает операцию. Retry использует тот же operation ID; при устаревшем fencing token действие отвергается.

## Guacamole как отдельная граница

Детальный протокол — [guacamole.md](guacamole.md). Guacamole VM не получает Proxmox/BMC токены и не имеет management маршрутов. Guacd не публикуется наружу. Параметры `hostname`, `protocol`, `port`, credentials разрешает backend по профилю, browser не задаёт произвольное назначение.

Новый connection grant короткоживущий, одноразовый и связан с browser session, user, Runtime, EnvironmentRun и authorization revision. Отзыв включает закрытие активного server-side tunnel; одного истечения grant недостаточно.

Публичные share links, Guacamole administration и ad-hoc connections выключены. Authenticated Teacher assistance разрешён только к собственному Environment с индикатором Student и scoped grant. Clipboard/file transfer/drive mapping/printing — явные permissions профиля. Запись экрана и содержимого терминала по умолчанию не включается: необходимость, доступ, уведомление и retention согласуются отдельно.

## Безопасные destructive workflows

Подтверждение удаления требуется самим продуктом по ТЗ §§6, 60, 63. Подготовка документации не выполняет такие операции и не требует их подтверждать сейчас.

Предлагаемый протокол: preview с явными IDs, размером, owner и dependency graph → короткоживущий confirmation token, связанный с actor/object versions → повторная проверка зависимостей → постановка durable operation → прекращение доступов → выполнение → audit результата.

Изменившийся dependency graph делает preview устаревшим. Нельзя удалить активный диск или template, от которого storage backend требует clone dependency. `safe_to_delete` — вычисленный результат с доказательствами, а не доверенный флажок от frontend.

Reconciliation лишь помечает orphan/missing. Неизвестная VM, диск без owner и частично созданный clone не удаляются автоматически. Temp/cache можно очищать автоматически лишь при доказанном владении и соответствующей policy; сомнительный объект становится кандидатом Admin.

Ни low disk, ни истёкший idle timeout не разрешают удаление учебных данных. Snapshot rollback/reset — также destructive workflow с показом потери изменений. SOFT_ARCHIVE означает закрытый доступ и сохранённые исходные disks/VM states, не наличие backup.

## Audit и наблюдаемость

Audit содержит actor/service identity, действие, target IDs, operation/correlation ID, авторизационную revision, время, результат и безопасную причину. Обязательные события ТЗ дополняются deny, reveal, grant issue/revoke, tunnel open/close, policy drift и cleanup preview/confirm.

Предложение: append-only доступ приложения к audit, выделенная retention policy и ограниченный доступ операторов. Audit события пишутся через durable outbox вместе с изменением домена. Пропуск доставки обнаруживается по последовательности и метрикам; secrets и содержимое сессии не логируются.

Log retention и session recording не смешиваются. Восстановление PostgreSQL, encryption keys и config выполняется по отдельному runbook с тестом restore. DEC-17 отключает backup в продукте; snapshots только Admin и не заменяют независимую копию. Hibernate state — рабочие данные, не backup.

## Проверки допуска

| Gate | Условие прохождения |
|---|---|
| SEC-01 | IDOR: чужие group/runtime/demo/grant IDs не раскрывают данные и не исполняют операции |
| SEC-02 | Отзыв membership/permissions действует на API, очередь jobs и активные access sessions |
| SEC-03 | Ограничения Agent: arbitrary shell/path/VMID/URL и replay отвергаются |
| SEC-04 | Proxmox token не может управлять объектами вне разрешённой области |
| SEC-05 | Root внутри LXC/VM не достигает management; весь набор NET gates пройден |
| SEC-06 | Credentials отсутствуют в логах/DTO/URL; reveal ограничен владельцем и audited |
| SEC-07 | Просроченные сертификаты, ключи и сессии отклоняются; rotation проверена |
| SEC-08 | CSRF, WebSocket origin, join brute force и rate limit проверены |
| SEC-09 | Delete/reset/archive с устаревшим preview и изменённой dependency graph блокируются |
| SEC-10 | Рестарт/потеря tunnel не уничтожает данные и не открывает доступ по умолчанию |
| SEC-11 | Backup restore включает DB, необходимые секреты и привязки provider objects |

Оставшиеся настройки production: MFA Admin, secret store/key custody, гостевой credential recovery и retention аудита. Identity, Internet, SOFT_ARCHIVE и отключённый backup уже определены пользователем; запись сессий по умолчанию выключена. Каждое получает владельца, решение и проверяемый критерий; отсутствие решения не подменяется небезопасным default.
