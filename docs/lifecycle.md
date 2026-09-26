# Жизненный цикл после уточнений пользователя

Приоритет: [decisions.md](decisions.md), особенно DEC-04,11–14,17. Исходное shutdown для всех заменено: QEMU hibernate-to-disk, LXC graceful shutdown с сохранением файлов.

## Три действия преподавателя

| Действие | Машины | Данные | Ресурсы |
|---|---|---|---|
| Завершить пару | QEMU HIBERNATED, LXC STOPPED; Demo по своему kind | Диски остаются; у VM остаётся RAM state | CPU/RAM освобождаются после подтверждения; storage остаётся занят |
| Архивировать окружение | Сначала тот же конец пары, затем запрет запуска | Те же Runtime, owners, disks и VM states на node | Disk commitments остаются; место не освобождается |
| Удалить окружение и данные | Доступ закрывается, машины останавливаются | Explicit plan удаляет disks, state files, snapshots, Demo | Ресурсы освобождаются по подтверждённым эффектам |

Разархивирование возвращает те же машины тем же текущим участникам; не делает новые клоны, не восстанавливает данные исключённых студентов и не запускает compute без admission. Template существующего Environment не меняется.

## Состояния

Environment сохраняет состояния CREATED/PROVISIONING/STOPPED/STARTING/RUNNING/STOPPING/DEGRADED/ERROR/DELETING/DELETED. STOPPED означает «нет занятого compute» и может агрегировать HIBERNATED VM и STOPPED LXC. UI уточняет способ сохранения. archive_state: NONE/ARCHIVING/ARCHIVED/RESTORING/FAILED; RESTORING означает снятие логического архива, не backup restore.

Runtime observation: ABSENT, STOPPED, STARTING, RUNNING, STOPPING, HIBERNATING, HIBERNATED, RESUMING, UNKNOWN, ERROR. desired state, observation timestamp/generation, operation state и archive status независимы. PAUSED_IN_RAM, если вообще наблюдается извне, не освобождает RAM и не считается корректным концом пары.

EnvironmentRun: один незавершённый запуск. Operation: QUEUED/RUNNING/WAITING_NODE/WAITING_RECONCILIATION/SUCCEEDED/FAILED/CANCEL_REQUESTED/CANCELLED. Сетевой timeout не откатывает выполненный provider effect.

## Start / resume

API быстро записывает Operation/outbox и возвращает 202. Worker обеспечивает READY и fresh inventory, проверяет ownership, booking interval, quota, конфигурацию и snapshots/admin policy. В транзакции создаётся Run, roster snapshot и allocations; отдельный admission охватывает Demo, network и storage для будущего сохранения RAM.

Существующая HIBERNATED VM возобновляется из того же state artifact. STOPPED LXC запускается с тем же filesystem. Отсутствующие Runtime текущих участников создаются один раз. Rejoin после подтверждённого удаления создаёт новую generation, а не обещает вернуть прежние данные.

До доступа проверить applied network revision, guest protocol readiness и новый Guacamole authorization. Сохранённые в RAM старые TCP-сессии не считаются действующими browser grants; reconnect выполняется заново. Ошибка resume не приводит к молчаливому удалению state file и холодному старту.

При partial provisioning Environment DEGRADED: готовые ресурсы сохраняются, неизвестные эффекты сверяются. Retry продолжает ту же операцию/Run и не делает второй clone.

## Завершение пары

1. Закрыть новый admission/open в этот Run; сохранить warning deadline в Operation, предупредить студентов и подключённых Teacher.
2. Закрыть Guacamole tunnels; дождаться подтверждения или отметить uncertainty.
3. Для каждой QEMU, включая QEMU Demo, сохранить memory/device state на поддержанный storage и подтвердить HIBERNATED без работающего VM process.
4. Для каждого LXC, включая LXC Demo, выполнить graceful shutdown и подтвердить STOPPED. Файлы остаются, процессы LXC не сохраняются.
5. Освободить compute allocations/restart holds и active slot только для подтверждённых эффектов; закрыть Run после всех машин/задач. Постоянные disks и RAM state commitments не освобождаются.

Proxmox документирует VM Hibernate как запись RAM на диск с остановкой VM и последующим восстановлением при запуске. Конкретные storage/capabilities проверяются на целевой установке. [Proxmox QEMU hibernation](https://github.com/proxmox/pve-docs/blob/master/qm.adoc#hibernation).

Сохранять RAM нужно иметь возможность уже при start admission: memory-state storage budget резервируется заранее, с provider overhead. Если hibernation всё же не завершилась, не заявлять, что данные сохранены или RAM освобождена. Требуются retry/reconciliation либо явно видимый force-stop с потерей несохранённой RAM. Пользователь разрешил force-stop неисправной VM, но он не заменяет успешную hibernation. Hard shutdown LXC не считать автоматически согласованным fallback.

## Idle во время пары

Ответ пользователя определяет полный конец пары, но не меняет требование idle внутри активного Run. Сохраняется проектный HOLD_FOR_RUN: idle-stopped guest удерживает restart budget до конца пары, чтобы вернувшийся студент мог продолжить. Active session, CPU/network activity или protected task запрещают idle stop; stale telemetry не означает idle.

Для VM idle hibernation сохраняет процессы, если policy включает это действие; LXC idle shutdown сохраняет файлы. Student не может запустить целиком STOPPED/ARCHIVED Environment. Open во время HIBERNATING/STOPPING ждёт завершения и заново проверяет run/booking.

## Исключение студента

DEC-11: teacher подтверждает exact plan удаления данных Student в выбранной Group, включая архивные Environment. Сначала membership переходит REMOVING и отзываются все доступы, затем typed stop/delete tasks удаляют disks, snapshots и RAM states. После подтверждения — REMOVED, data_deleted_at, tombstones и освобождение quota. Аккаунт и данные других Group не затрагиваются.

По окончании удаления повторное вступление даёт пустую новую generation от исходного template. Ban может дополнительно запретить rejoin. Ошибка storage/provider не возвращает доступ и не выдаётся за успешную очистку; оставшиеся объекты видны Admin.

## Node, Tuya и выключение

READY требует tunnel, Agent, Proxmox, storage, network, gateway и свежий inventory. Наблюдения независимы: фазы boot могут приходить в разном порядке. Board online и ACK Tuya не означают pc power ON.

Перед физическим shutdown под node lock устанавливается DRAINING и закрывается admission generation. Проверяются отсутствие compute/reservations/restart holds, активных sessions, provider/agent jobs, hibernation/snapshot/deletion tasks и работающих unmanaged guests. HIBERNATED VM с проверенным state file и остановленные LXC не мешают shutdown; постоянные диски и будущая бронь сами по себе не держат сервер включённым. Следующий ближайший слот учитывается политикой boot lead time.

Штатное выключение host — через Proxmox/Agent после drain. Tuya Reset/forceReset не штатный shutdown и не fallback от простого network timeout.

## Reconciliation и изменения

После рестартов сопоставляются DB desired state, provider inventory, tasks, machine generation, disks, hibernation state artifacts и Guacamole sessions. VMID один не доказывает ownership. Missing state artifact → ERROR/операторское решение, не автоматическое discard. Неизвестные VM/disks не уничтожаются.

Snapshot/rollback доступны только Admin и проверяют текущий hibernation lock/state: нельзя совмещать несовместимые операции. Изменение virtual hardware или template у HIBERNATED VM запрещено обычным PATCH. Для другого template создаётся новое Environment; существующий архив остаётся связанным со старой конфигурацией.
