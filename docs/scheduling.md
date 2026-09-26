# Планирование ресурсов и выполнение операций

Статус: проект для начала разработки, 16.09.2026. Продуктовый код ещё не реализован.

Основное ТЗ: `LAB_MANAGER_CODEX_SPEC.md`, прежде всего §20–29, §37–42, §46, §64, §66–67, §74. Слова «требование ТЗ» обозначают обязательное исходное поведение. «Проектное решение» обозначает предлагаемую конкретизацию; она не выдается за уже согласованную пользователем деталь. Архитектура сразу рассчитана на итоговую систему.

## 1. Инварианты

Требования ТЗ:

- Capacity рассчитывает backend; preview в UI не предоставляет гарантию запуска.
- Несколько преподавателей работают одновременно. Проверка и reservation выполняются одной транзакцией.
- RAM — жёсткий ресурс. CPU допускает конфигурируемую переподписку.
- DemoRuntime участвует в каждом расчёте наравне со студенческими машинами.
- Конец пары освобождает compute после подтверждённого QEMU hibernate/LXC shutdown; disks и VM states остаются.
- Потеря связи с Proxmox или Agent не означает выключение Runtime.
- Неопознанные VM/LXC после reconciliation не уничтожаются автоматически.

Проектные инварианты:

- PostgreSQL — единственный источник reservations, resource ledger, durable jobs и desired state. Redis используется для кеша, доставки событий и ускорения пробуждения workers; потеря Redis не теряет операции и не освобождает ресурсы.
- На каждое Environment допускается не более одного незавершенного EnvironmentRun и одной нетерминальной операции start, включая QUEUED/WAITING_NODE до создания Run. На Runtime допускается одна конфликтующая изменяющая операция. Это обеспечивается ограничениями БД и сериализацией aggregate, а не только проверкой в HTTP handler.
- Admission проверяет одновременно node capacity, лимиты владельца Environment, storage, права инициатора, профиль и revision конфигурации. Совместного владения Teacher нет; Admin transfer атомарно переносит budget.
- Все managed VM/LXC имеют выключенный Proxmox autostart. Запуск после перезагрузки Node допускается только через scheduler после reconciliation и повторного admission.
- Изменение ресурсов вне Lab Manager считается drift. Найденные внешние потребители включаются в capacity или блокируют admission до разбора; они не считаются бесплатными.
- Только свежий inventory с подтверждённой полнотой позволяет новые allocations. Последний известный inventory доступен в UI с отметкой времени, но не используется как обещание доступности.

## 2. Единицы и формулы

Проектное решение: внутри хранить RAM/swap в целых MiB, диски в bytes, CPU в целых milli-credits; наружу отдавать явные единицы. Поля ТЗ `memory_mb` и `disk_gb` при согласовании API получают недвусмысленное определение. Не смешивать GB/GiB и MB/MiB при конвертации.

Для Node:

```text
ram_budget_mib = physical_ram_mib
                 - host_reserve_mib
                 - infrastructure_reserve_mib
                 - safety_reserve_mib

ram_claimed_mib = running_allocations
                  + startup_reservations
                  + restart_holds
                  + uncertain_allocations
                  + external_workload_allowance

ram_admissible_mib = max(0, ram_budget_mib - ram_claimed_mib)

runtime_ram_claim = configured_guest_max_mib + runtime_overhead_mib
```

`runtime_overhead_mib` зависит от LXC/QEMU и подтверждается нагрузочным тестом. Общие процессы Guacamole, Agent, page cache и ограничения ZFS ARC, если ZFS выбран, учитываются в соответствующих резервах инфраструктуры/хоста; один расход не вычитается дважды. Swap и уменьшение фактического RSS не увеличивают допустимую RAM. Для VM резервируется максимальная настроенная память; ballooning не служит основанием переподписки RAM.

Расчёт из ТЗ в бинарных единицах:

| Потребитель | RAM |
|---|---:|
| 30 студентов × 768 MiB | 23 040 MiB = 22.5 GiB |
| DemoRuntime × 768 MiB | 768 MiB |
| Всего гостевая память | 23 808 MiB = 23.25 GiB |
| Остаток от бюджета 24 GiB | 768 MiB |

Следовательно, пример 32 GiB RAM с резервами 4+2+2 GiB **не доказывает**, что 30 студентов и demo поместятся: оставшиеся 768 MiB должны покрыть все дополнительные расходы Runtime, не включенные в общие резервы. Если demo имеет 2 GiB, гостевая память уже равна 24.5 GiB. Обязательный admission отклоняет превышение; фактическая вместимость определяется hardware inventory и тестом выбранных профилей.

### CPU credits

ТЗ задаёт credits, но не их единицу. Проектное решение: `1000 milli-credits = 1 CPU credit`; это единица admission, соответствующая разрешённому эквиваленту времени одного логического CPU, а не гарантированному выделенному ядру. В профиле хранится явная стоимость, по умолчанию равная положительному `cpu_limit × 1000`. `vCPU` задаёт видимую гостю параллельность, `cpu_limit` — верхнюю границу потребления, `cpuunits` — относительный приоритет; эти параметры не взаимозаменяемы. Такое различие соответствует [официальной документации Proxmox о CPU limits](https://github.com/proxmox/pve-docs/blob/master/qm.adoc#resource-limits).

```text
cpu_budget_mcredits = floor(eligible_logical_cpus × oversubscription_ratio × 1000)
cpu_claimed = active + reserved + restart_holds + uncertain + external_allowance
```

В примере ТЗ `28 × 1.5 = 42 credits` только если все 28 threads включены в `eligible_logical_cpus`. Конкретное значение ratio, исключаемые CPU и CPU headroom инфраструктуры задаёт Admin после измерений. Профиль с unlimited CPU не допускается без отдельной стоимости admission и явного административного разрешения. Admin override не обходит физическую RAM, опасные storage thresholds и правила изоляции.

### Лимиты преподавателя

RAM/CPU считаются по активным, зарезервированным и неопределённым вычислительным обязательствам всех Environment владельца, включая demo. `max_active_environments` включает STARTING, RUNNING, STOPPING и незавершённый DEGRADED Run, пока сохраняются claims. Disk quota учитывает также остановленные Runtime и их сохраняемые данные: stop не позволяет обойти `max_disk_gb`.

Снижение лимита ниже уже занятых ресурсов запрещает новые увеличения, фиксирует over-limit и уведомляет Admin. Оно само по себе не удаляет данные и не выключает занятия. Stop, отключение доступа и безопасное освобождение ресурсов должны оставаться доступны. Передача владения атомарно переносит квоты с проверкой нового владельца под блокировками обоих teacher ledgers.

## 3. Постоянные записи учёта

Это проектное расширение обязательной сущности Reservation; имена уточняются вместе с общей схемой БД.

| Запись | Назначение и обязательные данные |
|---|---|
| NodeResourceLedger | `node_id`, RAM/CPU budgets, claims, inventory revision/time, health, `admission_epoch`, `version` |
| TeacherResourceLedger | `teacher_id`, вычислительные claims, disk commitments, число активных Environment, policy revision |
| StorageResourceLedger | `pool_id`, логические обязательства, outstanding physical reservations, safety budget, inventory revision |
| Reservation | `id`, operation/run/runtime refs, `kind`, ресурсы, state, owner, config/policy revision, created/expiry timestamps |
| ResourceAllocation | Подтверждённое либо неопределённое потребление, origin reservation, observed generation, причина удержания |
| Operation / OperationStep | Desired transition, idempotency key, payload hash, actor, state, attempts, deadline, result, provider task refs |
| JobLease | Worker owner, `lease_until`, монотонный fencing generation, heartbeat |
| OutboxEvent | Событие/работа, записанные атомарно с изменением состояния; delivered/attempts |
| AgentCommandReceipt | `command_id`, payload hash, object generation, accepted epoch, progress/result, provider task reference |

Ledger — агрегат для admission; детализация allocations/reservations позволяет восстановить его и объяснить UI. Любое изменение детализации и агрегата выполняется одной транзакцией. Периодический invariant check выявляет расхождения; он не молча обнуляет неопределённые claims.

`Reservation` имеет фазы `HELD → DISPATCHED → CONVERTED` или `RELEASED`; при неопределённости — `RECONCILING`. Преобразование Reservation в Allocation атомарно убирает старую сумму и добавляет новую: ресурс не учитывается дважды. Логическое disk commitment продолжает существовать после остановки вычислительной Allocation.

## 4. Транзакционное admission

Проектное решение: короткие PostgreSQL транзакции с `READ COMMITTED` и явными `FOR UPDATE` над постоянными ledger rows. Все пути изменения ресурсов, включая Admin actions, idle, cleanup, resize и reconciliation, соблюдают один порядок:

```text
1. NodeResourceLedger — по node_id
2. TeacherResourceLedger — по teacher_id
3. StorageResourceLedger — по pool_id
4. Environment — по environment_id
5. Runtime / DemoRuntime — по стабильному object_id
6. Reservation / Allocation / Operation — по id
```

Если объект требует несколько узлов или владельцев, сначала определяется полный набор; IDs сортируются в каждом классе. Изменившийся набор означает rollback и повтор всего admission. Нельзя взять Environment lock, а потом «добавить» Node lock. Ledger rows создаются при регистрации Node/Teacher/Pool, поэтому отсутствующая строка не оставляет admission без блокировки.

PostgreSQL удерживает блокировки `FOR UPDATE` до окончания транзакции; одинаковый порядок захвата уменьшает риск deadlock. Даже при этом нужен ограниченный retry всей транзакции при deadlock/serialization conflict, с jitter и прежним idempotency key. [PostgreSQL: Explicit Locking](https://www.postgresql.org/docs/current/explicit-locking.html).

API принятие Environment start:

1. Аутентифицировать инициатора; проверить доступ, известное состояние, expected version и разрешения. Повтор idempotency key с другим payload — конфликт; с тем же payload возвращается прежняя Operation.
2. В короткой транзакции сериализовать aggregate, проверить отсутствие конфликтующей нетерминальной start Operation и создать `Operation(QUEUED)` вместе с outbox, desired intent и audit. На этой стадии нет Run, resource claims и сетевого вызова. Для взятых строк сохраняется относительный порядок Environment → Operation; API не захватывает затем resource ledgers в обратном порядке.
3. Немедленно вернуть `202 Accepted`, operation ID и status URL согласно [api.md](api.md). Это подтверждение долговечного принятия команды, **не reservation и не обещание capacity**. UI получает progress через SSE/WebSocket; HTTP connection больше не удерживается.

Worker admission той же Operation:

1. Получить job lease и свежий полный inventory вне ресурсной транзакции. При Node не READY перевести Operation в WAITING_NODE и выполнить разрешённый power workflow либо ждать разрешённого включения. Перед power-on повторно проверить соответствующее право. Boot не удерживает SQL locks и не создаёт compute claims по старым данным.
2. После READY в новой короткой транзакции взять перечисленные resource/object locks и запись **существующей** Operation последней. Проверить актуальные lease/generation, состояние/права/revisions, Node admission_epoch, fresh inventory, отсутствие DRAINING/exclusive conflict и отмены. State progress и наблюдаемое power state различаются, как в [lifecycle.md](lifecycle.md).
3. Снять roster snapshot активных участников группы. Учесть существующие Runtime, отсутствующие Runtime, demo и исключённых участников, данные которых пока сохраняются. Roster фиксируется в Operation/Run; более поздние вступления обрабатываются отдельным sync-members. Demo обязательна DEC-07 и включена в budget каждого Environment.
4. Рассчитать полный план: RAM/CPU для запуска, disk delta для новых/увеличиваемых дисков, обязательный hibernate-state budget QEMU, Admin snapshot/provisioning peak reservations, network prerequisites. Проверить все лимиты и storage gates из [storage.md](storage.md).
5. При успехе атомарно создать единственный Run и claims, обновить существующую Operation/steps и Environment state, записать outbox и audit. При capacity denial завершить Operation как FAILED с понятным результатом; Run и claims не создаются. Новая попытка после изменения условий — новая явно запрошенная операция; повтор прежнего HTTP-key не теряет старый результат.
6. Commit; исполнять typed steps вне транзакции. Runtime open/start внутри уже активного Run создаёт или продолжает свою Operation и delta admission, но не создаёт ещё один EnvironmentRun.

API preview возвращает `inventory_as_of`, `config_revision`, requested/available по каждой оси, demo отдельной строкой и machine-readable denial reasons. Preview не резервирует ресурсы; worker admission пересчитывает всё заново. При недостатке ресурсов нет частичного запуска группы по умолчанию: admission целиком проходит или отклоняется до создания/старта гостевых ресурсов. Разрешённое power-on могло уже произойти для получения свежего inventory; это отдельно видимый эффект и не означает успешного запуска Environment.

Exclusive mode — проектная трактовка разрешения ТЗ: node admission lease для одного Environment, получаемая тем же протоколом блокировок только после освобождения конфликтующих claims. Она не выключает чужие занятия автоматически и не даёт доступ к management network.

## 5. Durable execution, fencing, идемпотентность

Одна SQL transaction не может атомарно включать вызов Proxmox. Проектное решение — saga с durable steps, transactional outbox и безопасными повторениями. Система обеспечивает повторяемость операций, а не обещает недостижимое «ровно один сетевой запрос».

Worker забирает job через короткую транзакцию и `FOR UPDATE SKIP LOCKED`, назначает lease и увеличивает generation. `SKIP LOCKED` применим для потребителей очереди, но даёт неполную картину заблокированных строк и поэтому **не применяется к расчёту capacity**. [PostgreSQL: SELECT locking clause](https://www.postgresql.org/docs/current/sql-select.html#SQL-FOR-UPDATE-SHARE).

Перед сетевым вызовом durable step записывается как dispatch intent. Команда содержит `command_id`, operation/step IDs, node admission_epoch, Runtime generation, payload hash, expected state и fencing generation. Payload hash покрывает неизменяемую семантику действия; lease/fencing/attempt относятся к отдельно проверяемому transport envelope, чтобы законный повтор нового worker не изменял смысл прежней команды. Agent:

- аутентифицирует VPS и проверяет target Node, epoch, generation, срок и разрешённый тип операции;
- сохраняет receipt в устойчивом локальном журнале до начала side effect;
- сериализует конфликтующие команды одного Runtime/Environment;
- при повторе command ID и того же hash возвращает известный progress/result; иной hash отклоняет;
- отклоняет устаревшие поколения и команды старого boot/admission epoch;
- сохраняет provider task ID и до повторного create/delete/start сверяет inventory и фактический task result.

Локальный журнал Agent не является основной БД продукта; формат выбирается как часть реализации Agent. После перезапуска receipt и generation должны переживать рестарт. Если журнал утрачен, Agent входит в reconciliation, а не принимает старые команды как новые.

Fencing запрещает действия поколений ниже уже принятого Agent поколения, но не отменяет уже принятый Proxmox task. Само истечение worker lease не доказывает, что Agent уже увидел более новый fence. Поэтому до выдачи конфликтующего шага нового поколения Agent устанавливает новый fence, сверяет журнал и дожидается подтверждённого окончания старого task либо переводит объект в неопределённость. Неизвестные старые результаты удерживают reservation. Worker с истёкшей lease не может записать успешный результат поверх нового поколения: update выполняется с проверкой lease owner/generation.

VMID выделяется динамически под контролем Provider, фиксируется до create; сопоставление использует также immutable Lab Manager object ID и generation в managed metadata. Совпадение одного VMID не доказывает принадлежность. Потеря ответа после create означает inspect/adopt уже созданного managed объекта, а не выделение второго VMID и второго диска.

При сбое посередине запуска уже созданные диски не удаляются компенсацией автоматически. Успешно запущенные машины безопасно останавливаются либо Run остаётся DEGRADED согласно результатам отдельных steps. Вычислительные claims освобождаются только для подтверждённо остановленных объектов; дисковые остаются до отдельного утверждённого удаления. Повтор Run использует те же Runtime.

## 6. Leases, TTL и неопределённость

Job lease — право worker продолжать операцию. Resource reservation — обязательство scheduler. Это разные сроки и разные состояния.

| Ситуация | Решение |
|---|---|
| Reservation истекла, ни один dispatch intent не был создан | В транзакции отменить job и HELD; освободить ресурс |
| Есть dispatch intent, но ответ потерян | RECONCILING, claim сохраняется до доказанного результата |
| Worker умер после успешного Proxmox task | Новый worker восстанавливает receipt/inventory и конвертирует claim один раз |
| Истёк Agent heartbeat / WireGuard пропал | Node UNKNOWN; запрет нового admission; ресурсы остаются занятыми |
| Shutdown/hibernate отправлен, но stop не подтверждён | STOPPING/UNKNOWN; RAM и CPU не освобождаются |
| Доказан новый boot Node и полное inventory подтверждает выключенные Runtime | Reconcile освобождает соответствующие compute allocations; restart требует нового admission |
| Reservation TTL закончилась во время большого clone | Она не освобождается, пока provider task выполняется или результат неизвестен |

Подтверждение power-off может помогать закрыть compute claims только в согласованной модели доверия PowerProvider и при заблокированном автоматическом запуске. Одного `OFFLINE` или сетевого timeout недостаточно. Storage ownership/commitments от выключения Node не меняются.

## 7. Idle и гарантия повторного открытия

Требование ТЗ: активная Guacamole session, существенная CPU/network activity или защищённая фоновая задача запрещают idle stop. Потеря телеметрии не является отсутствием активности. Студент может повторно открыть автоматически остановленный Runtime внутри активного Environment.

Проектное решение по умолчанию: `restart_capacity_policy = HOLD_FOR_RUN`. Idle stop освобождает фактическое потребление, но вычислительная Allocation атомарно превращается в `restart_hold` того же размера. Остальные Environment не занимают этот бюджет, поэтому возвращение студента не проигрывает гонку другому преподавателю. Quota и число активных Environment сохраняются до завершения Run.

Дополнительная явная policy `RELEASE_ON_IDLE` может освобождать бюджет. Тогда open выполняет новое admission и вправе вернуть «недостаточно ресурсов»; нельзя обещать гарантированный повторный запуск. Это поведение требует продуктового согласования и не включается скрытно. Изменение policy отражается в UI преподавателя и audit.

Idle процесс: собрать свежие сигналы → вычислить continuous inactivity window → проверить policy и protected task lease → предупредить при необходимости → взять обычные resource/object locks → убедиться, что нет новой session/активности и Run открыт → закрыть новые подключения на время stop → graceful shutdown → подтвердить STOPPED → преобразовать/освободить claim. Open, столкнувшийся с STOPPING, ожидает завершения и затем проходит штатный restart.

Protected background task имеет owner, причину и ограниченный TTL; student не получает неограниченный способ отключить idle policy. Manual stop преподавателя остаётся отдельным действием: предупреждение и grace period по ТЗ, отзыв Guacamole sessions, graceful shutdown. Hard stop — отдельный разрешённый шаг с видимым риском потери несохранённых данных, а не автоматическое следствие сетевого timeout.

## 8. Node drain и выключение

Под Node ledger lock установить DRAINING и увеличить admission_epoch: это закрывает admission раньше проверки пустоты. Затем проверить отсутствие активных/переходных Run, compute reservations/restart holds, незавершённых Agent jobs, backup/snapshot/export jobs и Guacamole sessions. Постоянные disk commitments выключению не мешают: это хранимые данные, а не рабочие reservations.

Повторить проверку непосредственно перед power-off, включая Agent task registry и provider inventory. Действующий новый admission в DRAINING невозможен. При неизвестном inventory или обнаруженной внешней работающей VM автоматическое выключение запрещается. После выключения показать подтверждённый power state отдельно от «нет связи». Экстренное выключение Admin — отдельная аудируемая процедура.

## 9. Reconciliation

Reconciler запускается после старта VPS/Agent, восстановления тоннеля, периодически и после неопределённого результата. Он берет полное inventory Node/storage/provider tasks/Guacamole, маркирует его generation и сопоставляет с БД по managed immutable IDs, а не именам.

Под теми же locks reconciler корректирует observed state и ledger; желаемое состояние и история не переписываются задним числом. `Runtime missing` не означает «создать новый пустой»: сначала проверяются диски, tasks и ошибки inventory. `Unexpected running` получает compute claim и может блокировать следующие старты. `Orphan` регистрируется для Admin. Состояние Environment выводится из Run и Runtime, включая частичные ошибки, после чего восстанавливаются разрешённые Guacamole registrations. Audit содержит старое/новое состояние, источник доказательств и correlation ID.

Два reconciliation jobs не должны одновременно редактировать один Node epoch. Периодичность, freshness limit и operation timeouts конфигурируются и попадают в deployment settings; конкретные production значения фиксируются после измерения сети и времени загрузки сервера.

## 10. Проверки готовности реализации

| Проверка | Ожидаемый результат |
|---|---|
| Два Teacher одновременно запрашивают по 15 GiB при доступных 20 GiB | Только один получает reservation; второй видит точный отказ |
| Два старта разных Environment одного Teacher исчерпывают его quota | Teacher ledger блокирует превышение даже на разных Node |
| Два повтора start/open/sync-members | Один Run/Runtime/side effect; ответы привязаны к одной operation |
| Demo создаётся вместе с последним студентом | В capacity включён demo и его отдельный профиль |
| Worker crash до dispatch, после dispatch и после provider success | Нет утраченной задачи, повторного Runtime и преждевременного освобождения claims |
| Node теряет heartbeat при работающих VM | UNKNOWN, новые admission запрещены, RAM не считается свободной |
| Idle stop одновременно с open | Последовательный stop/restart без пропущенной reservation |
| Drain одновременно со start | Победитель определяется Node lock/epoch; shutdown не теряет новые jobs |
| Отзыв разрешения/смена владельца во время очереди | Revalidation блокирует новые неразрешённые side effects; безопасная остановка доступна |
| Runtime уже создан, но API ответ потерян | Reconciliation находит тот же объект и диск |
| Stop/Start через неделю, перезапуск VPS и Node | Файлы сохранены, используются прежние Runtime, admission пересчитан |
| 30 студентов + demo + Guacamole под целевой нагрузкой | RAM без переподписки, приемлемые latency и время старта подтверждены измерением |

Встроенный FakeProvider полезен для unit/integration сценариев отказа, но не заменяет обязательные проверки на выбранной версии Proxmox и реальном storage backend. Production deployment gates включают версии, capacity, полный run/stop/restart, потерю сети и восстановление.

## 11. Уточнения: календарь и конец пары

DEC-09: [bookings.md](bookings.md) добавляет интервальные compute claims. Они принимаются транзакционно в том же lock order; первое успешное подтверждение получает ресурс. При старте booking claim преобразуется в active allocation, не списывается повторно. Будущая бронь не освобождает фактически работающую машину по таймеру.

После пары QEMU hibernate-to-disk, LXC shutdown, включая Demo соответствующего kind. После подтверждения release всех run compute/restart holds; persistent disks и hibernate state bound продолжают занимать storage. HOLD_FOR_RUN относится только к idle внутри пары, а не к уже завершённому Run.

Teacher видит собственные claims и обезличенную доступность; не получает чужие Environment/teacher/booking IDs через capacity errors, SSE, детализацию или метрики. Квоты Admin сохраняются независимо от календаря.
