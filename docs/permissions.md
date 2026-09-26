# Права и приватность преподавателей

Основание: исходное ТЗ §§8,55 и [decisions.md](decisions.md). Приоритет имеют новые решения пользователя: независимые Teacher, обязательная Demo, teacher assistance, snapshots только Admin, kick с удалением данных.

## Авторизация

У каждого Teacher собственные Group, Environment, Demo и bookings. Другой Teacher не видит их названий, roster, Runtime, sessions, секретов и времени активности. Доступность календаря обезличена. Admin управляет всем через аудит. Co-teacher/delegation функции не входят в текущую поставку.

Student регистрируется сам и получает только роль Student. Повышение до Teacher/Admin выполняется административно; код группы не выдаёт роль. Пользователь может иметь несколько ролей, но доступ определяется ролью в текущем действии и принадлежностью объекта.

Предлагаемый recovery: Admin после проверки личности выдаёт одноразовое короткоживущее reset-разрешение; пользователь задаёт новый пароль сам. Сохраняется только digest разрешения; отзываются старые login/access sessions. Не требуются email self-service и общий временный пароль.

## Политики

TeacherPermissionPolicy имеет immutable TeacherPermissionPolicyRevision. PolicyPermission/PolicyLimit ссылаются на revision; TeacherPolicyAssignment выбирает действующую revision. Индивидуальные overrides нормализованы: ALLOW/DENY, отсутствие строки = INHERIT. Учётные лимиты типизированы; zero не означает unlimited. Системный запрет текущей поставки сильнее teacher override.

При выполнении длительной операции повторно проверяются actor/owner, policy revision и объект. Отзыв права не удаляет автоматически данные и не запрещает безопасное завершение занятия. Новая политика не превращает существующий guest в orphan.

## 20 permissions исходного ТЗ и их текущая семантика

| Permission | Текущая поставка |
|---|---|
| `can_create_groups` | Создание своей Group |
| `can_delete_groups` | Удаление своей Group после явного разрешения её зависимостей; без неявного удаления чужих данных |
| `can_create_lxc` | LXC student profiles в своём Environment |
| `can_create_qemu` | QEMU student profiles; отдельное demo exception не расширяет это право |
| `can_use_linux_profiles` | Допустимые Linux student profiles |
| `can_use_windows_profiles` | Допустимые Windows student profiles |
| `can_use_custom_profiles` | Только проверенные Admin custom profiles, не произвольные host parameters |
| `can_create_demo_vm` | Для активного Teacher должна существовать разрешённая Demo-конфигурация; политика не допускает Environment без Demo |
| `can_enable_group_network` | GROUP_LAN и разрешённые учебные сегменты внутри своего Environment |
| `can_change_network_policy` | Изменения в разрешённых границах; management deny не отменяется |
| `can_allow_internet_access` | Включение Internet согласно template/profile и административным ограничениям |
| `can_create_snapshots` | Сохранено имя из ТЗ, но Teacher/Student всегда DENY по DEC-17; snapshots только Admin |
| `can_restore_snapshots` | Teacher/Student всегда DENY; Admin с явным destructive plan |
| `can_delete_own_environments` | Окончательное завершение и удаление своего Environment/данных |
| `can_archive_environments` | SOFT_ARCHIVE без переноса и освобождения дисков |
| `can_override_idle_policy` | Только в границах разрешённого Admin диапазона, не продлевает чужую бронь |
| `can_override_resource_limits` | Ограниченный административный допуск внутри hard capacity; не обходит чужую бронь, safety reserve и 10% storage stop |
| `can_power_on_node` | Запрос штатного включения через Tuya с дедупликацией |
| `can_request_node_shutdown` | Запрос DRAINING; не право прервать чужую работу |
| `can_use_exclusive_mode` | Только при выданном праве и отсутствии конфликтующих броней; обычная бронь резервирует ресурсы, не весь сервер |

Все 20 имён сохранены для traceability, но строки snapshots изменены пользователем. Admin snapshot action не эмулирует Teacher override. Дополнительные capability для помощи и подтверждённого удаления участника выражаются отдельными системными действиями с ownership scope.

## 6 limits из ТЗ

| Limit | Учёт |
|---|---|
| `max_lxc_per_environment` | Student LXC и pending creation; Demo отдельно по профилю, но всегда в ресурсном бюджете |
| `max_vm_per_environment` | Student VM и pending creation |
| `max_total_ram_mb` | Зарезервированная/активная RAM Teacher, с Demo и overhead; после пары освобождается только по факту stop/hibernate |
| `max_cpu_credits` | Вычислительный budget, включая Demo и reservations; не синоним vCPU |
| `max_disk_gb` | Постоянные disks + VM state budget + admin snapshots соответствующих машин; архив также учитывается |
| `max_active_environments` | Открытые активные Run, включая переходные/неопределённые состояния |

Модель временных booking claims не суммируется с тем же активным allocation повторно. Квота отвечает на вопрос «сколько разрешено Teacher», календарь — «доступно ли это в нужный интервал». Admin задаёт значения после получения hardware inventory.

Actor, owner и billing_teacher_id записываются раздельно для аудита, но в обычном teacher workflow владелец и бюджет совпадают. Передача Group/Environment возможна только Admin как отдельная операция; два Teacher не получают совместное управление. При передаче atomic quota checks и отзыв прежних grants обязательны.

## Demo и student assistance

Каждый запускаемый Environment содержит N student Runtime и одну Demo. Teacher должен иметь хотя бы один разрешённый demo profile. Отдельный DemoProfileGrant позволяет QEMU Demo даже при запрете student QEMU; student endpoint не может выдать себя за Demo. Forbidden second demo и cross-environment binding проверяются backend.

Teacher может открыть машину Student только своего Environment. Возможность наблюдать/вводить в той же сессии реализуется authenticated assistance через Guacamole, с индикатором «преподаватель подключён» и audit. Student не получает публичную share link или право приглашать третьих лиц. Если shared tunnel не поддержан проверенной комбинацией, UI сообщает это; второй независимый shell не называется совместным просмотром.

Разрешение помогать не выдаёт service passwords/Proxmox tokens. Собственный guest password Student и внутренний IP могут показываться через scoped UI. Teacher guest access проходит Guacamole; раскрытие пароля Student не следует автоматически из возможности подключиться.

## Исключение и окончательное удаление

DEC-11 заменяет исходное Remove membership only. Кнопка означает «Исключить и удалить данные студента в этой группе». Preview показывает все его Runtime в активных и архивных Environment этой Group, disks, snapshots, hibernation states и размер. После подтверждения: закрыть membership/grants, закончить сессии, остановить машины, удалить только перечисленные объекты, проверить физическое завершение и освободить quota.

Уже существующие admin snapshots этих машин включаются в approved deletion plan как зависимые данные; Teacher не получает самостоятельное API управления snapshots. Неизвестные/внешние зависимости блокируют удаление и передаются Admin, а не расширяют scope операции. Повторное вступление после удаления создаёт новый guest generation без старых данных. Неудачное удаление видимо как DELETING/FAILED, доступ уже закрыт.

Ban только запрещает повторное вступление; если требуется kick, вызывается этот же destructive workflow. Не создавать скрытый альтернативный путь удаления данных через read/update membership.

Окончательное удаление Environment включает Student Runtime и Demo. Завершение пары и SOFT_ARCHIVE никогда не означают удаление. Нехватка storage не разрешает автоматическую очистку чужих/студенческих данных.
