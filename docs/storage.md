# Хранение данных, квоты и безопасная очистка

Статус: проект для начала разработки, 16.09.2026. Storage backend пока неизвестен. Архив выбран: SOFT_ARCHIVE; backup выключен, snapshots только Admin, QEMU сохраняет RAM state по [decisions.md](decisions.md).

Основное ТЗ: `LAB_MANAGER_CODEX_SPEC.md`, §5–6, §28–36, §60, §63, §66, §74. Требования исходного ТЗ обязательны; явно обозначенные проектные решения уточняют их для реализации и не подменяют ответы владельца системы о физической инфраструктуре.

## 1. Что сохраняется

Конец пары сохраняет QEMU RAM/device state на диск и останавливает VM; LXC выключается с сохранением filesystem. Следующий start/resume использует прежний Runtime. Hibernate state обязателен для VM, но не является backup или пользовательским snapshot.

Исключение Student по DEC-11 отзывает доступ и удаляет его Runtime/disks/snapshots/hibernate states во всех Environment этой Group, включая архивные, по exact plan. Данные других Group и аккаунт не затрагиваются. Остановка Environment, архивирование Group, понижение квоты или low disk не являются разрешением на уничтожение данных.

TemplateVersion фиксируется в Environment и Runtime. Обновление каталога профилей не изменяет существующие диски. Template upgrade in place запрещён; reset/rollback остаются отдельными destructive действиями, rollback/snapshots только Admin. Export/backup текущей поставки отключён.

## 2. StorageProvider и возможности backend

Проектное расширение архитектуры: `StorageProvider` предоставляет inventory, capabilities, расчёт операции, зависимости, scan candidates и типизированные storage действия; ProxmoxVirtualizationProvider сохраняет ответственность за VM/LXC. Произвольные команды shell через публичный API не добавляются.

Capabilities на `StoragePool`: supported runtime kinds, image formats, full/linked clone, disk snapshot, RAM snapshot, backup/export/restore, resizing, thin provisioning, physical attribution accuracy, thin metadata sensor, atomic provider locks. Unsupported и unknown — разные результаты; отсутствие данных не трактуется как нулевое потребление.

Для LVM-thin блоки выделяются по мере записи; логический размер томов может превышать физическое место. Он поддерживает snapshots/clones, но является локальным storage, не общим между узлами. Это свойства backend, а не разрешение Lab Manager переподписывать диски. [Официальная документация Proxmox LVM-thin](https://github.com/proxmox/pve-docs/blob/master/pve-storage-lvmthin.adoc).

Для операций с гостями выбирается Proxmox storage ID (например, `student-lvm`), который Proxmox сопоставляет со своим VG и thin pool. Lab Manager не требует ручной привязки этого хранилища к PV/VG UUID. Наблюдаемые LVM UUID помогают диагностировать неожиданную замену или пересоздание тома, но не являются основанием ни для выделения ресурсов, ни для удаления данных. Безопасный admission определяется свежестью и полнотой inventory, разрешённым storage ID, учётом обязательств, резервами места и проверками сети.

Окончательный выбор между LVM-thin, ZFS и file-based storage требует инвентаризации дисков, RAID, существующей разметки, объёма, Proxmox version и требований восстановления. До этого нельзя записывать production commands форматирования/переразметки или обещать одинаковую snapshot semantics для всех backend.

## 3. Три независимых вида учёта

| Вид | Что измеряет | Когда освобождается |
|---|---|---|
| Virtual commitment | Обещанная пользователю логическая ёмкость дисков, включая остановленные Runtime | После подтвержденного удаления/уменьшения; не после stop |
| Physical consumption | Реально занятые блоки pool/filesystem по свежему inventory | Когда backend подтвердил фактическое освобождение |
| Outstanding reservations | Ресурсы для принятых, но ещё не отражённых в inventory операций и их временного пика | При атомарной конвертации в учтённый объект либо доказанной отмене |

Дополнительно отдельно измеряется thin metadata capacity, если backend её имеет. Свободные data blocks не гарантируют свободное место для thin metadata. Для данного backend обязателен подтверждённый способ наблюдения data и metadata utilization; неизвестное значение делает allocation health неопределённым, а не зелёным. Sensor, единицы и emergency thresholds проверяются на стенде выбранной версии.

`StorageUsageSnapshot` хранит: backend/pool ID, provider generation, collected_at, completeness, total/used/free bytes, thin data/metadata totals и usage либо `not_applicable`, категории, attribution confidence и collection errors. «Не поддерживается» отображается явно; `null` не превращается в 0.

UI показывает quota, actual usage, outstanding reservations и worst-case commitments раздельно. Нельзя складывать виртуальные 15 GiB диска и физические 2 GiB того же диска и называть результат занятым местом. Нельзя прибавлять snapshot apparent size к фактическому размеру base, если backend уже включил общие блоки в оба значения.

### Логические квоты

Проектное решение: `max_disk_gb` владельца ограничивает суммарные virtual disk commitments его Runtime/demo плюс обязательный бюджет QEMU hibernate state и разрешённых Admin checkpoints. SOFT_ARCHIVE продолжает использовать тот же budget; дополнительного archive export budget нет. Каталог shared templates учитывается на pool, а не многократно как реальное использование каждого преподавателя.

```text
teacher_disk_commitment = existing_runtime_virtual_bytes
                          + pending_new_or_resize_virtual_delta
                          + vm_hibernate_state_commitments
                          + admin_snapshot_growth_commitments

teacher_disk_commitment <= teacher_disk_limit
```

Число сохраняемых окружений косвенно ограничивается дисковой квотой; `max_active_environments` ограничивает только compute runs. Каждый EFI/TPM/cloud-init/дополнительный управляемый том учитывается в плане соответствующего Runtime. Unmanaged bind mounts и произвольные внешние диски студентам не предоставляются без отдельного безопасного профиля.

### Физический admission без двойного списания

Действуют два независимых gate:

1. **Текущее место:** `actual_free - unobserved_outstanding_peak - requested_peak > safety_floor` и pool health допускает allocation. Уже отражённые в inventory bytes удаляются из `unobserved_outstanding_peak` в той же транзакции, в которой подтверждается observation. Потерянный ответ не является основанием снять резерв.
2. **Будущие обязательства:** консервативный bound хранимых данных вместе с принятыми новыми обязательствами должен помещаться в configured usable budget. Он включает полные virtual quotas Runtime, template bounds, snapshot growth bounds, archive bounds, временный пик операций, инфраструктурный budget и safety reserve. Pending → provisioned меняет владельца записи bound, но не добавляет второй такой же расход.

Проектный default: физическая переподписка virtual commitments выключена. Thin provisioning улучшает actual usage, но не продаёт одну физическую ёмкость нескольким независимым гарантиям. В worst-case bound не прибавляется ещё раз весь `actual_used` тех же управляемых дисков. Для внешних/неатрибутированных данных используется отдельный bound не меньше измеренного использования с запасом роста.

Bound — запас на наихудший случай, а не точный breakdown: например, full virtual quota каждого linked clone может консервативно перекрывать shared template blocks. UI называет его «резерв под обязательства» и указывает модель/ограничения, а не обещает безусловную защиту от заполнения любого storage. Гарантия действительна только при контроле всех writers, включении snapshot/metadata/temporary overhead и проверенной модели выбранного backend. Экономию shared blocks можно показывать по достоверной метрике, но не использовать для уменьшения резерва без такой модели.

Согласованного, действительно гарантированного bound без знания backend и unmanaged writers может не быть. Тогда provider возвращает `CAPACITY_UNKNOWN`, новые allocations блокируются и Admin видит причину. Простое значение `df free` не подменяет этот gate.

Все gate выполняются под теми же Node → Teacher → Storage → Environment locks, что описаны в [scheduling.md](scheduling.md). Runtime start без создания диска сохраняет старое virtual commitment; он не резервирует весь диск повторно. Отдельно проверяются write headroom, storage health и ресурсы compute.

## 4. Пороговые состояния и противоречие ТЗ

В §32 пример: 20% free — warning, 10% — critical, 7% — запрет новых Runtime. В §74 allocation должен блокироваться уже при critical. Пользователь разрешил расхождение: новый allocation блокируется при 10% free (DEC-15).

Действующее решение DEC-15: `warning_free_percent=20`, `critical_free_percent=10`, `allocation_stop_free_percent=10`; 7% — дополнительный emergency уровень. Пороги конфигурируемы, но запрет allocation должен наступать не позже critical: для шкалы свободного места `allocation_stop_free_percent >= critical_free_percent`. В UI все значения подписаны как **процент свободного места**.

| Состояние | Предлагаемое поведение |
|---|---|
| HEALTHY, free >20% | Admission по всем quotas/bounds и freshness |
| WARNING, 10% < free <=20% | Alert, оценка тренда, cleanup recommendations; allocations только при сохранении post-operation запасов |
| CRITICAL, free <=10% | Запрет новых дисков, resize, snapshots, local archive/export и новых запусков с риском роста; существующие sessions не разрываются автоматически |
| EMERGENCY, free <=7% | Срочный alert; запрет операций увеличения места; только согласованные recovery/cleanup действия; нет автoудаления данных |
| UNKNOWN / storage unavailable / metadata unhealthy | Fail-closed для новых allocations; разрешены read-only диагностика и безопасные действия восстановления |

Проверять и текущий процент, и прогноз после операции: free=12% не разрешает clone, после которого останется 8%. Нужны также абсолютный byte reserve, metadata limits, inodes для file backend и гистерезис выхода из alert; процент сам по себе недостаточен. Exact metadata thresholds и absolute floor определяются после выбора storage и теста.

Гость может продолжать писать после блокировки новых allocations. Поэтому одних alert и порога 10% недостаточно: обязательны заранее удержанные bounds, мониторинг темпа роста и время реакции. В emergency автоматически удалять данные запрещено. Автоматическое принудительное выключение работающих гостей также не предполагается без отдельной согласованной policy: graceful shutdown может требовать записи, hard stop несёт риск повреждения. Runbook должен описывать действия Admin для выбранного backend.

## 5. Snapshots

Действующее DEC-17: checkpoints доступны только Admin, ограничены retention/capacity/dependencies; Teacher и Student не могут создавать/восстанавливать snapshots. Автоматические snapshot triggers выключены. Проектное решение: snapshot policy содержит `enabled`, allowed triggers, `max_count_per_runtime`, `max_total_growth_bytes`, max age, protected flag, RAM-state allowance и auto-retention authorization.

Snapshot может быть маленьким при создании и значительно вырасти после изменения live-диска. На admission резервируется не только текущая metadata/initial size, но и верхняя граница удерживаемых исторических блоков. Консервативный default — до полной суммы virtual disks на каждый checkpoint плюс backend overhead. При трех checkpoints нельзя считать, что их суммарный будущий размер обязательно меньше одного диска. Если выбранный backend предоставляет более точную доказанную границу, её контракт и проверка фиксируются отдельно.

RAM snapshots выключены по умолчанию; при отдельном разрешении резервируется RAM-state файл и compatibility metadata. Нужен supported-capability check для всех томов, политика guest quiescing и результат `crash_consistent` либо `application_consistent`. Несколько Runtime окружения получают snapshot batch, но это не обещание атомарного снимка распределённой БД всей группы.

В текущей поставке разрешён только явный Admin manual checkpoint с batch budget. Автоматические before-start/after-stop/before-reset triggers не включаются. Auto snapshot, которому не хватает места, помечается `SKIPPED_CAPACITY`, а не создаётся без резерва. Reset или rollback, при котором snapshot является обязательной защитой, останавливается при невозможности такой защиты; UI не выдаёт успешный reset. Обычный start не требует snapshot, если профиль не требует checkpoint.

Retention сначала формирует candidates. Автоудаление checkpoints со студенческими данными выключено по умолчанию; его можно включить только явной scoped policy с audit. Protected snapshots и dependencies исключаются всегда. Уменьшение количества snapshots не считается освобождением места до подтверждения backend и inventory.

Linked clones зависят от base/template; Proxmox запрещает удалять исходный template при существующих linked clones. Поэтому graph зависимости должен проверяться при удалении template/snapshot и перед reset. [Proxmox: Copies and Clones](https://github.com/proxmox/pve-docs/blob/master/qm.adoc#copies-and-clones).

## 6. Breakdown и dependency graph

Категории ТЗ: Runtime disks, snapshots, templates, archives, other. Для backend с shared blocks проектное дополнение — `shared_or_unattributed`. Сумма взаимно исключающих известных физических категорий плюс эта категория равна total used в пределах указанной погрешности. Если нельзя определить физическое использование конкретного snapshot, показывать «точный размер недоступен» и estimate range, а не выдуманное число.

Обязательные рёбра: Runtime → disk; Runtime/Demo → snapshot; linked clone → base disk/template version/snapshot; archive → manifest и objects; Operation → temporary volumes/tasks; configured Runtime → все дополнительные managed disks. `safe_to_delete` — вычисленный результат с причинами и inventory timestamp, а не постоянный Boolean, которому можно доверять спустя неделю.

Для каждого object фиксируются immutable ID, provider locator, managed ownership proof, owner/group/environment, created_at/last_used_at, logical/physical bytes с accuracy, generation, dependencies, protected/legal policy flags при наличии. Имя вида `vm-123-disk-0` — locator, не доказательство владельца и не право на удаление.

## 7. Логический архив и отключённый backup

DEC-04: SOFT_ARCHIVE. Завершить текущий Run (VM hibernate/LXC shutdown), закрыть доступ и новые starts, записать ARCHIVED. Все disks, hibernate states и Admin snapshots остаются на Proxmox; quota не освобождается. Разархивирование возвращает те же машины прежним текущим участникам и снимает запрет запуска; новый запуск требует compute admission/booking.

Исключённый с удалением Student не восстанавливается через unarchive; restore — не откат истории membership. Cold export, archive uploads и backup jobs отключены DEC-17. ArchiveProvider/ArchiveArtifact допускаются как будущий extension contract, но не как заявленная функция текущей поставки.

Backup средствами Proxmox может настроить оператор независимо; Lab Manager не создаёт копии автоматически. Snapshot и hibernation не заменяют backup. Без отдельной копии DB VPS нельзя гарантировать восстановление аккаунтов, membership и ownership только из дисков Proxmox; выбранное отключение backup не означает гарантию аварийного восстановления.

## 8. Safe cleanup: preview → подтверждение → revalidation

Требование ТЗ: destructive API принимает явные IDs; нет `delete all`; проверяются зависимости; активный Runtime disk не удаляется. Неизвестные orphan не удаляются автоматически. Дополнительное подтверждение требуется независимо от объёма свободного места.

Проектное API-дополнение к §60 — общий `POST /api/destructive-plans` из [api.md](api.md) с cleanup action и explicit object IDs. Для storage candidates он разрешает candidate ID в immutable storage object identity и сохраняет обе связи. Ответ содержит immutable plan ID, список конкретных объектов/зависимостей, owner/group/environment, состояние, expected reclaimed bytes или range, неизвестные величины, риск, expires_at и confirmation fingerprint.

Fingerprint связывает actor, action, explicit IDs, immutable provider IDs, object generations/config versions, dependency graph, policy revision, relevant inventory epoch и срок. Подтверждение `POST /api/storage/cleanup` передаёт plan ID, fingerprint, тот же набор IDs и idempotency key. Plan атомарно потребляется одной Operation; повтор с тем же key возвращает эту Operation. API быстро записывает Operation/outbox и возвращает `202 Accepted`; worker выполняет revalidation и удаление асинхронно. Wildcards, фильтр «все старше N дней» вместо IDs и подмена IDs после preview запрещены.

Выполнение:

1. Проверить actor/permissions, актуальность плана и exact payload. Устаревший/изменённый план вернуть как conflict с требованием нового preview.
2. Под общими resource/object locks повторно проверить ownership, состояние Runtime, отсутствие session/operation, полный graph зависимостей и retention/protection policy. Mark deleting блокирует новый start, snapshot, attach и clone.
3. Agent непосредственно перед provider mutation повторно сверяет immutable identity, current provider config/disk UUID/generation и доступные provider locks. Пропавшая связь означает остановку операции, а не доверие старому preview.
4. Удалять по проверенному dependency order. При изменении graph или внешнем Proxmox task вернуть conflict. Повтор command ID не удаляет другой объект с повторно использованным VMID/именем.
5. Подтвердить provider completion, обновить inventory, затем освободить disk commitment/reservation и записать audit. Не обещать reclaimed bytes до измерения.

Между SQL и Proxmox нет общей транзакции. Пока операция работает, нельзя разрешать managed mutations вне Agent; внешние Admin изменения обнаруживаются как drift. Fencing и revalidation минимизируют окно, но не дают невозможную атомарность с независимыми ручными изменениями storage. Production runbook должен запрещать параллельное ручное удаление managed volumes.

Для bulk каждый ID получает итог SUCCESS/FAILED/CONFLICT/SKIPPED с причиной. Общая ошибка не скрывает уже завершённые удаления и не запускает весь batch заново без idempotency. Destructive confirmation не является многоразовым правом на произвольные будущие объекты.

## 9. Orphan detection и ограниченная автоочистка

Reconciliation сопоставляет БД с полным VM/LXC, volumes, snapshots, Guacamole и provider task inventory. Отсутствие Runtime в БД может означать восстановление БД из старой копии, частичный inventory, ещё незавершённый clone или реальную внешнюю машину.

Orphan получает immutable observation, first/last_seen, evidence, предполагаемого владельца и причину. Неполный scan не порождает вывод «владелец исчез». Повторные полные сканы могут повысить confidence, но не дают разрешение удаления. Admin выбирает adopt, preserve/quarantine либо preview/delete с проверками.

Автоочистка по ТЗ допустима для expired temp files, connection artifacts, caches и failed clone remnants. Проектная граница безопасного failed clone: есть подтверждённый operation owner, immutable creation identity, доказанный failure, отсутствие любого Runtime/dependency/provider task и отсутствие пользовательской записи в этот объект. Если эти доказательства неполны, это orphan для ручного решения. Возраст или похожее имя сами по себе недостаточны.

Retention активного student Runtime не удаляет его без отдельного явного разрешения. Остановленный Runtime не становится безопасным только из-за длительного простоя. Внешняя ручная правка database ownership не служит автоматическим доказательством безопасной очистки.

## 10. Проверки готовности реализации

| Сценарий | Ожидаемый результат |
|---|---|
| 30 новых Runtime создаются двумя workers | Общие reservations не превышают quota и pool bounds |
| Остановка всех Runtime | Compute освобождается согласно run policy; virtual disk quota сохраняется |
| Actual 2 GiB у диска quota 15 GiB | UI не показывает «занято 17 GiB»; physical и logical раздельны |
| Clone завершился, ответ потерян | Claim сохраняется до reconcile; физические bytes не списываются дважды |
| Старт новой группы переводит free 12% → 8% | Admission отклонён до записи дисков |
| Free ровно 20%, 10%, 7% и metadata близка к пределу | Границы включены согласно таблице, alerts детерминированы |
| Три snapshots + интенсивная перезапись всех блоков | Worst-case budget удержан заранее; нет необеспеченного роста |
| Linked clone зависит от старого template | Удаление template блокируется с указанием зависимости |
| Snapshot/current disk делят blocks | Breakdown не удваивает shared physical usage |
| Runtime запускается после cleanup preview | Confirmation conflict; активный диск не удаляется |
| VMID/volume name переиспользован после preview | Fingerprint/identity mismatch, новый объект не затронут |
| Batch удалён наполовину, worker перезапущен | Повтор продолжает оставшиеся IDs без расширения scope |
| Неизвестная VM или orphan disk найдены | Только candidate/alert, без автоматического delete |
| Попытка backup/export Teacher или Admin | Функция отключена, новых копий нет |
| SOFT_ARCHIVE → unarchive | Те же Runtime/disks/student bindings, storage quota не менялась |
| Node недоступен во время cleanup | Нет destructive операции по устаревшему inventory |

Нагрузочные и failure tests выполняются на выбранном storage backend, включая near-full data/metadata, interruption clone/snapshot/export, write growth, restore и restart VPS/Agent. Без этих доказательств функции snapshots/cleanup/archive нельзя считать готовыми только по успешным unit tests.

## 11. Место под hibernation VM

На запуске QEMU резервировать место для будущего RAM/device state с консервативным bound по configured maximum RAM и provider overhead. Оно удерживается пока VM существует и поддерживает hibernation, без повторного списания actual file поверх того же commitment. У LXC такого state budget нет. Для 30 VM по 768 MiB и Demo того же размера это до 23.25 GiB только memory component, до overhead; фактический файл зависит от provider.

State artifact может расти во время сохранения; нельзя ждать конца пары, чтобы впервые проверять место. Admission учитывает peak и пороги; если storage уже заполнен или state save завершился ошибкой, показывать failed hibernate и удерживать compute до доказанного stop. Не выполнять automatic discard или cold boot вместо обещанного resume. При resume/удалении освобождение фактических блоков проверяется отдельно, permanent bound следующей hibernation остаётся до удаления/изменения policy.
