# Интеграция Apache Guacamole

Статус: целевой дизайн и обязательный интеграционный spike. ТЗ v2.0, §§13–15, 37–42, 58–59, требует browser-only, единый вход, временное разрешение, session tracking и завершение активных сеансов. Готовая совместимая интеграция пока не реализована.

## Размещение и границы

Предложение: отдельная инфраструктурная VM на Proxmox содержит Guacamole web application, guacd и строго ограниченные сервисы интеграции. VPS публикует только HTTPS и проксирует browser traffic через WireGuard. PostgreSQL Lab Manager остаётся источником доменной истины; Guacamole не становится второй системой пользовательских аккаунтов.

Guacd соединяется только с Runtime по SSH/RDP/VNC. Web application выполняет аутентификацию и управляет Guacamole tunnels. Browser передаёт ввод и получает изображение/терминал через протокол Guacamole. [Официальная архитектура](https://guacamole.apache.org/doc/gug/guacamole-architecture.html).

Guacamole VM и её сервисы не получают Proxmox root credentials, Agent identity либо права BMC. Не подключать эту VM к каждой учебной L2-сети как доверенный участник: использовать узко разрешённый маршрутизируемый доступ. Guest-initiated доступ к инфраструктурным сервисам закрыт.

Доступ к собственному гостевому паролю разрешён ТЗ §15 и описан в [security.md](security.md). Он не требует выдачи прямого endpoint для входа и не разрешает раскрытие инфраструктурных secrets.

## Выбор механизма — проектное предложение

Использовать собственное небольшое Java-расширение `lab-manager-auth` на официальном `guacamole-ext`, закреплённое за проверенной версией Guacamole. Оно отвечает за exchange одноразового grant, выдачу только разрешённого Connection, server-side tunnel tracking, отзыв и закрытие соединений. Python остаётся основным языком backend/agent; Java-компонент — явная зависимость полного продукта, не скрытая «магия token».

Guacamole поддерживает внешнюю аутентификацию через расширения; штатная JSON authentication принимает подписанные/зашифрованные connection data и поле expiry. Документированное expiry ограничивает приём этих данных, но само по себе не доказывает одноразовость, отзыв или завершение уже открытого туннеля. [External authentication](https://guacamole.apache.org/doc/gug/external-auth.html), [JSON authentication](https://guacamole.apache.org/doc/gug/json-auth.html).

Поэтому JSON auth не считать готовой реализацией требований Lab Manager. Не строить flow на недокументированном предположении «Guacamole принимает произвольный JWT». HTTP header auth также не заменяет объектную авторизацию и lifecycle сеанса.

Расширение предоставляет ровно разрешённую конфигурацию, а не каталог всех machines. Оно само запрещает неразрешённые обращения к объектам, поскольку permissions UI не обеспечивают enforcement вместо `UserContext`. API расширений позволяет управлять `ActiveConnection` и закрывать underlying tunnel. Версия расширения должна совпадать с проверенной версией Guacamole; wildcard совместимости не использовать. [Guacamole extension API](https://guacamole.apache.org/doc/gug/guacamole-ext.html).

## Предлагаемые доменные сущности

Они дополняют обязательный `GuacamoleConnection`, а не меняют понятия Group/Environment/Runtime.

| Сущность | Минимальные поля и назначение |
|---|---|
| `GuacamoleConnection` | Runtime/Demo reference, gateway_id, provider_connection_ref, protocol, secret_ref, configuration_revision; постоянная регистрация |
| `AccessGrant` | hash nonce, user_id, browser_session_id, runtime reference, environment_run_id, authorization_revision, expires_at, consumed_at, revoked_at |
| `AccessSession` | id, grant_id, user_id, runtime reference, gateway_id, provider_tunnel_id, connected_at, disconnected_at, last_activity_at, last_seen_at, state, close_reason |
| `GatewayLease` | gateway_id, epoch, expires_at, last_heartbeat; ограничивает работу при потере control plane |
| `AccessEvent` | session_id, gateway_epoch, monotonic sequence, event type, observed_at; дедупликация/доставка |

`provider_connection_ref` и `provider_tunnel_id` различаются: одна постоянная регистрация может дать несколько отдельных сеансов. Количество одновременных сеансов задаётся policy; нет публичных sharing links; authenticated teacher assistance создаёт отдельный scoped grant.

Значение nonce генерируется криптографически стойко. В БД хранится только hash. Исходное значение кратко живёт в защищённом exchange и не появляется в URL/referrer/access log. TTL и максимальное время отзыва задаются конфигурацией; предлагаемый начальный TTL grant — 60 секунд, решение требует spike.

## Последовательность открытия

1. Browser вызывает `POST /api/runtimes/{id}/open` с обычной Lab Manager session и CSRF-защитой.
2. Backend проверяет пользователя, membership/принадлежность своему Teacher, ownership, разрешённый EnvironmentRun и policy; DemoRuntime использует свою проверку.
3. Если Runtime остановлен по idle при активном Environment, применяется разрешённый restart use case с transactional reservation. Student не запускает полностью остановленный Environment обходным вызовом `open`.
4. Пока запускается Runtime, API возвращает operation ID и состояние ожидания. Frontend получает прогресс по SSE/WebSocket. Grant не выдаётся до readiness и проверки network applied revision.
5. Backend создаёт краткоживущий `AccessGrant` на точный Runtime и run revision. Ответ содержит только безопасный launch handle/путь и срок, без VMID, внешнего guest endpoint или service secret; собственный private IP разрешён в отдельном Runtime DTO.
6. Browser делает same-origin POST exchange. Предлагаемый launch broker на VPS проверяет browser session и вызывает backend consume атомарно: grant ещё действителен, не использован/не отозван, identity и текущие права совпадают.
7. Broker передаёт утверждённую identity расширению по аутентифицированному внутреннему каналу. Browser не может сам подписывать trusted headers или задавать `hostname`/порт.
8. Расширение ещё раз проверяет разрешение на подключение; получает параметры из server-side registration и запускает tunnel. Создание `AccessSession` фиксируется до признания сессии активной.
9. Browser открывает Guacamole client через тот же публичный HTTPS origin. Guacamole auth material не превращается в долговременный bearer token браузера.
10. Server-side события отмечают реальное подключение, активность и закрытие. Нажатие кнопки «Открыть» не считается состоявшимся сеансом.

Предложение launch broker требует отдельной проверки способов обмена с выбранным Guacamole release. Нужно доказать, как внутренний Guacamole auth token связывается с защищённой browser session и не попадает в URL/логи. Документация проекта не должна представлять этот broker как уже готовый штатный компонент Guacamole.

При retry после потери ответа одноразовый grant не открывается повторно. Backend может вернуть тот же существующий launch/session для той же авторизованной операции либо потребовать новый grant после проверки; выбор должен быть детерминированным и протестированным.

## Отзыв и завершение

Причины: logout, блокировка User, отзыв membership, отзыв teacher assignment/permission, остановка Environment, удаление/архивирование, reset/rollback Runtime, истечение политики сеанса, административный revoke.

Порядок: зафиксировать revoked authorization revision → перестать выдавать grants → отметить pending grants отозванными → отправить close конкретных `AccessSession` → закрыть underlying tunnel → подтвердить закрытие событием. Один logout HTTP или истёкший launch token не закрывают TCP-туннель автоматически.

Предложение SLA: не более 15 секунд при работающем канале управления; при его потере локальная gateway lease истекает не позднее 60 секунд. Эти значения — проверяемые цели, а не обещание upstream. Обновление lease не может заново разрешить уже отозванную session revision.

Если gateway недоступен, backend показывает `REVOKE_PENDING/UNKNOWN`, запрещает новое открытие и сохраняет resource accounting. Агенту можно поручить изолировать/остановить Runtime по отдельному workflow, но нельзя заявить «сеанс закрыт» до наблюдения. При восстановлении прежде всего синхронизируется revoked state.

Завершение пары: teacher выбирает немедленное завершение либо предупреждение с задержкой → UI/Guacamole overlay уведомляет студентов → новый access закрывается → активные tunnels закрываются → QEMU hibernate / LXC graceful shutdown → диски и VM state сохраняются. При force stop после timeout операция требует policy и отражается как риск потери несохранённых гостевых данных.

## Session tracking и idle

Guacamole extension listeners поддерживают события аутентификации и жизненного цикла туннеля. Это подходящая точка интеграции, но сами события не предоставляют автоматически всю требуемую модель активности пользователя. [Event listeners](https://guacamole.apache.org/doc/gug/event-listeners.html).

Предлагается server-side счётчик осмысленного keyboard/mouse/terminal input в обёртке tunnel без записи содержимого. Отдельно хранить `last_seen_at` транспорта и `last_activity_at` взаимодействия: ping, keepalive, графические обновления и фоновый stdout не считаются человеческим вводом.

Открытый активный Guacamole-сеанс сохраняет Runtime по правилу ТЗ §37 даже при низком CPU. При отсутствии сеанса idle policy также учитывает CPU/network, protected task и grace interval. Browser heartbeat — недостоверный дополнительный сигнал, не основание немедленного shutdown.

Если session telemetry потеряна, состояние `UNKNOWN` временно блокирует idle shutdown и выключение node. Reconciliation сверяет активные tunnels и epochs gateway. После рестарта gateway прежние session IDs нельзя считать живыми по старой DB записи или повторно привязывать к новому процессу.

Events отправляются по аутентифицированному каналу с outbox/retry и последовательностью. Дубликат не создаёт второй сеанс. Отсутствующее disconnect восстанавливается по gateway inventory и lease; browser unload не используется как единственный механизм.

## Reverse proxy и браузер

Проксирование должно поддерживать WebSocket upgrade и streaming HTTP tunnel без задерживающей буферизации. Apache Guacamole отдельно описывает настройку reverse proxy и HTTP fallback. Оба разрешённых transport пути обязаны проходить одинаковую авторизацию. [Reverse proxy Guacamole](https://guacamole.apache.org/doc/gug/reverse-proxy.html).

Предлагаемый baseline:

- Публичная страница запуска — same origin с Lab Manager; допустимые origins фиксированы.
- Удалить пришедшие от Internet trusted identity headers перед добавлением серверных.
- Выделить служебный exchange endpoint, недоступный из Internet в обход broker.
- Secure/HttpOnly cookies, CSRF-защита, origin checks; нет tokens в localStorage или query string.
- CSP/frame-ancestors допускает только продуктовый сценарий; выбор встроенного окна или отдельной вкладки согласуется с проверенными headers.
- Отключить кэширование token/launch/reveal responses; редактировать логи, исключив bearer material.
- Только profile-approved clipboard, upload/download, drive redirection и printing.
- Запретить Guacamole admin UI, ad-hoc connections, user management и публичные sharing links для Student/Teacher. Разрешён только Lab Manager teacher-assistance flow в собственном Environment.

SSH host keys и RDP server identities проверяются по утверждённым template/runtime правилам. Не включать универсальное игнорирование сертификатов ради readiness. Способ первичного доверенного enrollment и смены host identity при reset проверяется на реальных протоколах.

## Обязательный spike до реализации пользовательского доступа

Spike — небольшой стенд с реальным Guacamole/guacd/расширением и тестовым guest. Его артефакты затем становятся version-pinned интеграционными тестами и production adapter, а не временным отдельным способом входа.

| Проверка | Успешный результат |
|---|---|
| GUAC-01: версии | Зафиксированы совместимые web/guacd/guacamole-ext/JDK/build versions и image digests |
| GUAC-02: единый вход | Lab Manager login открывает только разрешённый Runtime без второго пользовательского аккаунта/пароля |
| GUAC-03: replay | Одновременный consume одного grant открывает не более одного разрешённого launch |
| GUAC-04: identity | Чужой grant/browser session/Runtime ID и изменения параметров отвергаются |
| GUAC-05: TOCTOU | Отзыв между выдачей grant, consume и connect запрещает подключение |
| GUAC-06: revoke | Уже открытый SSH/RDP/VNC tunnel реально закрывается в заданное время |
| GUAC-07: lifecycle | Logout, membership remove, Environment stop/reset и блокировка пользователя закрывают доступ |
| GUAC-08: tracking | Connect/disconnect/input/keepalive различаются; retry/duplicate не искажают историю |
| GUAC-09: partition | Потеря VPS/WireGuard, рестарт gateway и истечение lease не оставляют бессрочный доступ |
| GUAC-10: proxy | WebSocket и, если включён, HTTP fallback проходят одинаковые auth/origin/revoke проверки |
| GUAC-11: secrets | Нет service secrets/guest endpoint/auth tokens в URL, логах и обычных API DTO |
| GUAC-12: нагрузка | 30 одновременных browser sessions с измеренными CPU/RAM/latency и graceful stop |

Отдельно отрицательными тестами подтвердить отсутствие Guacamole permissions на редактирование connections и перечисление чужих machines. Сессия другого пользователя не должна становиться доступной через подстановку provider connection/tunnel ID.

Если выбранная версия не позволяет закрывать tunnel, отслеживать активность и выполнить безопасный exchange, adapter не допускается к production. Решение возвращается к проектированию официального расширения; прямой SSH/RDP и общий Guacamole аккаунт не являются fallback.

## Артефакты готовности

До включения доступа реальным студентам должны существовать: закреплённая compatibility matrix, extension source/build, broker contract, ACL manifest, результаты GUAC/NET/SEC gates, runbook ротации secrets, revoke/reconcile runbook и измеренный infrastructure reserve для scheduler.

Требования по отзыву и наблюдаемости проходят вместе: нельзя считать доступ безопасным, если кнопка «завершить» только обновляет строку DB, а фактический tunnel продолжает работать.

## Teacher assistance: совместная сессия

DEC-10 разрешает Teacher подключаться к машине своего Student. Guacamole поддерживает sharing текущего active connection; это позволяет присоединиться к тому же отображению/терминалу. [Официальный Guacamole sharing](https://guacamole.apache.org/doc/gug/using-guacamole.html#sharing-the-connection). В Lab Manager не выдаём штатную публичную ссылку: grant привязан к аутентифицированному Teacher, Runtime, active tunnel, mode=view/control и revision.

Student видит индикатор присутствия Teacher; подключение/смена mode/отключение аудируются без записи содержимого. У двух участников одного tunnel свои AccessSession; CPU/RAM Runtime не резервируются дважды, gateway session overhead учитывается. Закрытие student tunnel завершает dependent assistance; Teacher не получает бессрочное подключение по старому grant.

Для SSH второй независимый login не видит старый shell, поэтому он не засчитывается как совместный просмотр. Для RDP нельзя полагаться на второй OS login, который может дать другой desktop или вытеснить пользователя. Проверяется join того же guacd connection через extension. Если комбинация unsupported, показывать ограничение; отдельное teacher connection не обещает совместный экран. Нагрузка включает дополнительных Teacher viewers.
