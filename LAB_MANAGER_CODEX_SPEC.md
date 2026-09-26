# Lab Manager — итоговая техническая спецификация для разработки

Версия: 2.0

Этот документ является основным техническим заданием проекта. Codex должен считать описанную ниже архитектуру целевой и разрабатывать сразу итоговый продукт. Допустима поэтапная реализация, но нельзя строить временную архитектуру, которую затем придётся заменять.

---

# 1. Назначение системы

Нужно разработать систему удалённого управления учебным сервером виртуализации.

Физический сервер:

- находится за NAT;
- не работает 24/7;
- включается удалённо;
- работает под Proxmox VE;
- предоставляет студентам персональные LXC или QEMU VM;
- должен поддерживать ориентировочно 30 одновременных лёгких Linux-окружений.

Публичная VPS работает постоянно и является Control Plane лаборатории.

Студенты и преподаватели работают только через публичный HTTPS-интерфейс.

Студенты не получают прямой SSH, RDP, VNC, VPN или доступ к Proxmox. Доступ к студенческим машинам выполняется исключительно через Apache Guacamole в браузере.

---

# 2. Главная пользовательская модель

Основные пользовательские сущности:

```text
Admin
Teacher
Student
Group
Environment
EnvironmentRun
Runtime
DemoRuntime
```

Пользовательская логика строится вокруг двух постоянных объектов:

```text
Group
Environment
```

`Group` — постоянная учебная группа.

`Environment` — постоянная конфигурация учебного окружения для группы.

Пара или конкретное использование окружения не является главным объектом интерфейса. Внутри backend для истории и аудита используется сущность `EnvironmentRun`.

---

# 3. Главный сценарий

## 3.1. Создание группы

Преподаватель создаёт группу:

```text
РПРБ-21
```

Система генерирует короткий код:

```text
K7F3Q2
```

Преподаватель выводит код на экран.

QR-коды не использовать.

Студент:

1. входит в свой аккаунт;
2. открывает страницу `Вступить в группу`;
3. вводит код;
4. подтверждает вступление.

После этого студент постоянно привязан к группе.

На следующих занятиях код повторно вводить не нужно.

Преподаватель может:

- включить или выключить возможность вступления;
- сгенерировать новый код;
- удалить студента из группы;
- заблокировать самостоятельное повторное вступление;
- видеть список участников.

Код группы не заменяет аутентификацию.

---

# 4. Окружение

Преподаватель создаёт для группы `Environment`.

Примеры:

```text
Linux / Git
C# Development
Docker Lab
PostgreSQL
Windows Administration
Computer Networks
```

Одновременно у одной группы может существовать несколько окружений.

Например:

```text
РПРБ-21

├── Linux Basic
├── C# Development
├── Docker
└── PostgreSQL
```

Окружение содержит:

- группу;
- владельца-преподавателя;
- тип: LXC или QEMU VM;
- template;
- template version;
- RAM;
- swap;
- vCPU;
- CPU limit;
- disk quota;
- сетевую политику;
- persistent policy;
- idle policy;
- Guacamole protocol;
- разрешение snapshots;
- демонстрационную машину преподавателя;
- список студенческих Runtime;
- состояние.

Окружение существует между занятиями.

---

# 5. Жизненный цикл окружения

Состояния:

```text
CREATED
PROVISIONING
STOPPED
STARTING
RUNNING
STOPPING
DEGRADED
ERROR
DELETING
DELETED
```

## 5.1. Первый запуск

При первом запуске окружения:

1. проверить доступность физического сервера;
2. проверить разрешения преподавателя;
3. рассчитать capacity;
4. зарезервировать ресурсы;
5. определить студентов группы;
6. найти уже существующие Runtime этого Environment;
7. создать отсутствующие Runtime;
8. создать DemoRuntime преподавателя;
9. настроить сеть;
10. зарегистрировать подключения Guacamole;
11. запустить машины;
12. выполнить readiness checks;
13. перевести Environment в `RUNNING`.

## 5.2. Завершение пары

Преподаватель нажимает:

```text
[Завершить работу окружения]
```

По умолчанию это означает:

```text
RUNNING
  ->
STOPPING
  ->
graceful shutdown всех Runtime
  ->
STOPPED
```

Runtime не удаляются.

Их виртуальные диски и файловые системы сохраняются.

На следующем занятии преподаватель запускает то же Environment, и каждый студент продолжает работу со своего предыдущего состояния.

Для LXC сохраняется файловая система контейнера.

Для VM сохраняется виртуальный диск.

Сохранять оперативную память машины между занятиями по умолчанию не требуется.

`Suspend` может быть добавлен как отдельная функция, но базовая модель persistence строится на выключении машины с сохранением диска.

---

# 6. Удаление окружения

Когда окружение больше не нужно, преподаватель с соответствующим правом или Admin может его удалить.

Перед удалением показывать:

```text
Environment: Docker Lab
Students: 28
Runtime disks: 71 GB
Snapshots: 19 GB
Total: 90 GB
```

Варианты:

```text
Удалить Runtime и данные
Архивировать данные
Отменить
```

Удаление должно требовать дополнительного подтверждения.

Нельзя удалять студенческие данные автоматически только потому, что storage заполнен.

---

# 7. EnvironmentRun

В UI преподаватель работает с `Environment`.

Backend при каждом запуске создаёт `EnvironmentRun`.

Он нужен для:

- истории;
- аудита;
- учёта времени;
- статистики;
- восстановления после сбоя;
- понимания, кто использовал ресурсы;
- хранения состояния конкретного запуска.

Поля:

```text
id
environment_id
started_by
started_at
stopped_at
state
allocated_resources
active_students
failure_reason
```

Преподавателю не обязательно показывать термин `EnvironmentRun`.

В UI можно показывать:

```text
История запусков
```

---

# 8. Права преподавателей

Admin должен иметь гибкую систему разрешений преподавателей.

Недостаточно ролей `Admin / Teacher`.

Нужны permission policies.

Пример:

```text
Teacher A:
  LXC: allowed
  QEMU VM: denied
  Windows profiles: denied
  Docker VM: denied
  custom resources: denied

Teacher B:
  LXC: allowed
  QEMU VM: allowed
  custom network: allowed
```

## 8.1. PermissionSet преподавателя

Минимально поддержать:

```text
can_create_groups
can_delete_groups

can_create_lxc
can_create_qemu

can_use_linux_profiles
can_use_windows_profiles
can_use_custom_profiles

can_create_demo_vm

can_enable_group_network
can_change_network_policy
can_allow_internet_access

can_create_snapshots
can_restore_snapshots

can_delete_own_environments
can_archive_environments

can_override_idle_policy
can_override_resource_limits

can_power_on_node
can_request_node_shutdown

can_use_exclusive_mode
```

Также лимиты:

```text
max_lxc_per_environment
max_vm_per_environment
max_total_ram_mb
max_cpu_credits
max_disk_gb
max_active_environments
```

Admin может назначать:

- готовую permission policy;
- индивидуальные overrides.

---

# 9. EnvironmentProfile

Преподаватель обычно выбирает готовый профиль.

Примеры:

| Profile | Type | RAM | CPU limit | Disk | Access |
|---|---|---:|---:|---:|---|
| Linux Light | LXC | 512 MB | 0.5 | 10 GB | Guacamole SSH |
| Linux Basic | LXC | 768 MB | 1.0 | 15 GB | Guacamole SSH |
| Linux Dev | LXC | 1024 MB | 1.5 | 20 GB | Guacamole SSH |
| Linux Desktop | LXC/VM | 1536 MB | 1.5 | 20 GB | Guacamole RDP/VNC |
| Docker Lab | VM | 2048 MB | 2.0 | 30 GB | Guacamole SSH |
| Windows Lab | VM | 3072–4096 MB | 2.0 | 40 GB | Guacamole RDP |

Профиль также определяет:

```text
runtime kind
template
template version
network defaults
idle policy
persistence mode
Guacamole protocol
allowed teacher permissions
```

Если у преподавателя отсутствует разрешение на QEMU, VM-профили для него вообще не показываются.

---

# 10. Демонстрационная машина преподавателя

Каждое Environment должно поддерживать отдельную `DemoRuntime`.

Она предназначена для демонстрации преподавателем через проектор.

По умолчанию:

```text
1 Environment
=
N student Runtime
+
1 DemoRuntime
```

DemoRuntime:

- принадлежит преподавателю;
- открывается только через Guacamole;
- может иметь отдельный профиль ресурсов;
- сохраняет состояние между запусками Environment;
- может быть сброшен в состояние template;
- может иметь snapshots;
- может быть подключён в ту же учебную сеть, что и студенты;
- может быть изолирован от студентов при необходимости.

Право создания DemoRuntime настраивается Admin отдельно.

Если преподавателю запрещено создавать обычные QEMU VM, Admin всё равно может отдельно разрешить ему DemoRuntime определённого профиля.

---

# 11. Сеть между студентами

Environment имеет `NetworkPolicy`.

Обязательные режимы:

```text
ISOLATED
GROUP_LAN
```

## 11.1. ISOLATED

Каждый Runtime:

- имеет доступ в Интернет, если разрешено;
- не видит Runtime других студентов;
- не видит management network;
- не видит Proxmox;
- не видит LAN организации.

Используется по умолчанию.

## 11.2. GROUP_LAN

Runtime одного Environment могут обращаться друг к другу.

Пример:

```text
student01 10.60.12.11
student02 10.60.12.12
student03 10.60.12.13
teacher   10.60.12.2
```

Это позволяет проводить лабораторные по:

- клиент-серверным приложениям;
- сетям;
- распределённым приложениям;
- БД;
- web services;
- multiplayer/network interaction.

При этом:

```text
Environment A
```

не должно видеть:

```text
Environment B
```

если Admin отдельно этого не разрешил.

## 11.3. Management isolation

Во всех режимах запретить доступ студентов к:

```text
Proxmox management subnet
Lab Node Agent
storage management
BMC/IPMI
VPS management ports
WireGuard control addresses
```

---

# 12. Возможные дополнительные NetworkPolicy

Архитектура должна позволять позже добавить:

```text
NO_INTERNET
CUSTOM_ACL
TEAM_NETWORKS
SHARED_SERVICE_NETWORK
```

Но `ISOLATED` и `GROUP_LAN` обязательны в основной версии.

---

# 13. Доступ студентов только через Apache Guacamole

Это жёсткое требование.

Студент не получает:

- прямой SSH;
- прямой RDP;
- VNC endpoint;
- VPN;
- IP для самостоятельного подключения;
- Proxmox Console;
- SPICE;
- shell физического сервера.

Единственный пользовательский путь:

```text
Browser
   |
 HTTPS
   |
  VPS
   |
 reverse proxy / WebSocket
   |
 WireGuard
   |
 Apache Guacamole
   |
 SSH / RDP / VNC
   |
 Student Runtime
```

Студент может находиться за любым NAT.

Физический сервер также может находиться за NAT.

Публичным остаётся только VPS.

---

# 14. Размещение Guacamole

Предпочтительная схема:

```text
Physical Server
├── Proxmox
├── Lab Node Agent
└── Guacamole Gateway
```

VPS проксирует Guacamole через WireGuard.

Причины:

- Guacamole находится рядом с Runtime;
- внутренние SSH/RDP/VNC подключения не выходят в Интернет;
- management credentials не нужно хранить в браузере;
- сервер за NAT остаётся закрытым.

Guacamole UI не должен использоваться как отдельная пользовательская система аккаунтов.

Lab Manager:

1. аутентифицирует пользователя;
2. проверяет право на Runtime;
3. выдаёт временный connection token;
4. перенаправляет в Guacamole.

---

# 15. Credentials Runtime

Студент должен видеть пароль машины, чтобы мог действовать от рута в своей виртуалке\контейнере что там будет. 

Lab Manager / Guacamole может использовать:

- автоматически созданные credentials;
- SSH keys;
- internal service account;
- временные пароли;
- connection parameters из backend.

Credentials хранятся серверной частью.

---

# 16. Группы

Group:

```text
id
name
owner_teacher_id
join_code
join_enabled
created_at
archived_at
```

GroupMember:

```text
group_id
student_id
joined_at
status
```

Один Student может состоять в нескольких группах.

Один Teacher может управлять несколькими группами.

Admin может:

- передать группу другому преподавателю;
- добавить второго преподавателя;
- архивировать группу.

---

# 17. Код группы

Требования:

- короткий;
- удобный для ввода;
- case-insensitive;
- исключать неоднозначные символы при генерации;
- уникальный среди активных кодов;
- можно регенерировать;
- можно отключить;
- иметь rate limit на попытки подбора.

Пример допустимого алфавита:

```text
ABCDEFGHJKLMNPQRSTUVWXYZ23456789
```

Не использовать:

```text
0 O 1 I
```

После вступления код больше студенту не нужен.

---

# 18. Поведение при появлении нового студента

Если студент вступил в Group после создания Environment:

```text
Environment STOPPED:
    Runtime создаётся при следующем запуске

Environment RUNNING:
    Teacher может нажать "Добавить новых участников"
    либо Runtime создаётся автоматически по policy
```

Новый Runtime получает ту же Environment configuration.

---

# 19. Поведение при удалении студента из группы

Удаление Student из Group не должно сразу удалять его Runtime.

Варианты:

```text
Remove membership only
Archive student Runtime
Delete Runtime and data
```

По умолчанию:

```text
Remove membership only
```

Данные остаются до решения преподавателя/Admin.

---

# 20. Физический сервер

VPS работает 24/7.

Физический сервер имеет состояния:

```text
OFFLINE
POWER_REQUESTED
POWERING_ON
BOOTING
TUNNEL_UP
PROXMOX_ONLINE
AGENT_ONLINE
READY
DRAINING
SHUTTING_DOWN
UNKNOWN
ERROR
```

Сервер считается `READY`, когда:

- WireGuard доступен;
- Proxmox отвечает;
- Lab Node Agent отвечает;
- storage доступен;
- bridge/network доступны;
- scheduler получил свежий resource inventory.

---

# 21. Удалённое включение

Использовать абстракцию:

```text
PowerProvider
```

Поддерживаемые реализации:

```text
IPMI/BMC
WoL via permanent LAN Agent
Smart power relay
Custom provider
```

Если сервер выключен, WireGuard физического сервера отсутствует.

Поэтому команда включения не должна зависеть от самого Proxmox.

---

# 22. Главный экран преподавателя

Если сервер выключен:

```text
Server: OFFLINE

[Включить сервер]
```

После включения:

```text
POWERING_ON
BOOTING
TUNNEL_UP
PROXMOX_ONLINE
AGENT_ONLINE
READY
```

После `READY` показывать:

```text
Мои группы
Мои окружения
Активные окружения других преподавателей
Свободные ресурсы
```

---

# 23. Работа нескольких преподавателей

Сервер не имеет простого состояния:

```text
занят / свободен
```

Несколько преподавателей могут одновременно использовать ресурсы.

Пример:

```text
Environment: Linux Basic
Teacher: Иванов
Students: 20

Environment: Networks
Teacher: Петров
Students: 8

RAM allocated: 21.2 / 24 GB
CPU credits: 25 / 42
```

Scheduler рассчитывает доступный остаток.

---

# 24. Resource Scheduler

Перед запуском Environment backend обязан рассчитать:

- RAM;
- CPU credits;
- storage;
- DemoRuntime;
- уже активные Environment;
- reservations;
- safety reserve.

Frontend не рассчитывает capacity самостоятельно.

---

# 25. RAM policy

RAM считать жёстким ресурсом.

Пример:

```text
Physical                  32 GB
Host reserve               4 GB
Infrastructure reserve     2 GB
Safety reserve             2 GB

Student capacity           24 GB
```

30 × 768 MB:

```text
22.5 GB
```

---

# 26. CPU policy

CPU можно oversubscribe.

Пример:

```text
28 logical threads
scheduler capacity = 42 credits
```

Коэффициент должен быть configurable.

Каждый EnvironmentProfile использует определённое число credits.

---

# 27. Transactional reservations

Обязательно предотвращать гонку двух преподавателей.

Нельзя:

```text
Teacher A увидел 20 GB free
Teacher B увидел 20 GB free

оба запускают по 15 GB
```

Использовать транзакционную reservation:

```text
BEGIN
lock resource ledger
recalculate capacity
reserve
COMMIT
```

---

# 28. Persistent Runtime

Основная модель для пользовательского сценария:

```text
Student
  |
Environment
  |
Runtime
```

Runtime создаётся один раз и живёт до удаления Environment.

На паре:

```text
STOPPED -> RUNNING
```

После пары:

```text
RUNNING -> STOPPED
```

Виртуальный диск сохраняется.

Это позволяет студенту продолжить работу через неделю с того же места.

---

# 29. Template Versioning

Environment сохраняет конкретную template version.

Пример:

```text
linux-basic:v3
```

Обновление profile до:

```text
linux-basic:v4
```

не должно автоматически изменять существующее Environment.

Преподаватель/Admin может выполнить:

```text
Upgrade Environment
```

только через контролируемый workflow.

---

# 30. Snapshots

Snapshots использовать как checkpoints, а не как основной механизм persistence.

Автоматические варианты:

```text
Before Environment Start
After Environment Stop
Manual Teacher Checkpoint
Before Reset
```

Не создавать snapshots бесконтрольно.

Snapshot policy должна учитывать storage usage.

---

# 31. Storage Monitoring

Это обязательная часть продукта.

На Proxmox Node отслеживать:

```text
physical disk usage
storage pool usage
free space
reserved space
thin pool usage
snapshot usage
runtime disk usage
template usage
archive usage
```

UI Admin:

```text
Storage

Total:      1.8 TB
Used:       1.31 TB
Free:       490 GB
Reserved:   150 GB

Runtime disks     870 GB
Snapshots         210 GB
Templates          85 GB
Archives          120 GB
Other              25 GB
```

---

# 32. Storage thresholds

Настраиваемые пороги:

```text
warning_threshold
critical_threshold
allocation_stop_threshold
```

Пример:

```text
20% free -> WARNING
10% free -> CRITICAL
7% free  -> запрещено создавать новые Runtime
```

Нельзя ждать полного заполнения storage.

---

# 33. Storage Cleanup

Admin должен иметь страницу очистки.

Показывать объекты, которые занимают место:

```text
Old snapshots
Deleted/archived Environment
Stopped Runtime
Orphan disks
Unused templates
Old template versions
Archives
Failed clone remnants
```

Каждый объект:

```text
owner
group
environment
size
last_used_at
created_at
dependencies
safe_to_delete
```

---

# 34. Cleanup recommendations

Система может рекомендовать:

```text
12 snapshots older than 90 days — 48 GB
3 archived environments — 71 GB
2 unused template versions — 12 GB
1 orphan disk — 15 GB
```

Но автоматическое удаление студенческих данных запрещено по умолчанию.

Допустима автоматическая очистка только для явно безопасных объектов:

- expired temp files;
- failed clone remnants;
- expired connection artifacts;
- caches по policy.

---

# 35. Retention Policies

Admin может задать:

```text
snapshot retention
archive retention
temporary runtime retention
log retention
audit retention
```

Student Runtime активного Environment нельзя удалять retention policy без явного разрешения.

---

# 36. Orphan Detection

Reconciliation должен находить:

```text
VM/LXC без Runtime в DB
Runtime в DB без VM/LXC
disk без владельца
snapshot без Runtime
Guacamole connection без Runtime
```

Orphan не удаляется сразу.

Он попадает в:

```text
Admin -> Storage -> Orphans
```

---

# 37. Idle Runtime Policy

Во время запущенного Environment Runtime можно автоматически останавливать по idle.

Но не только по CPU.

Учитывать:

```text
active Guacamole connection
CPU load
network activity
protected background task
last interaction
```

Пример:

```text
Guacamole active
CPU 0.2%
-> KEEP RUNNING

Guacamole closed
CPU 80%
-> KEEP RUNNING

Guacamole closed
CPU 0.2%
idle 30 min
-> STOP
```

Если Runtime был остановлен по idle, студент при следующем открытии:

```text
[Запустить и открыть]
```

---

# 38. Завершение Environment преподавателем

Преподаватель видит:

```text
28 Runtime

25 idle
3 active
```

При завершении:

```text
3 студента всё ещё работают.

[Предупредить и завершить через 5 минут]
[Завершить сейчас]
[Отмена]
```

Перед остановкой Guacamole показывает предупреждение студенту.

---

# 39. Demonstration Runtime

Преподаватель на странице Environment видит:

```text
Демонстрационная машина

[Запустить]
[Открыть]
[Перезапустить]
[Snapshot]
[Сбросить]
```

Открытие происходит через тот же Guacamole.

Никакого прямого RDP/SSH.

---

# 40. Apache Guacamole

Guacamole является единственным access layer для Student и Teacher Runtime.

Нужно интегрировать:

```text
Lab Manager Auth
  ->
authorization check
  ->
temporary connection authorization
  ->
Guacamole
```

Нельзя заставлять пользователя повторно логиниться в Guacamole отдельной учётной записью.

---

# 41. Guacamole connection lifecycle

При открытии Runtime:

1. проверить пользователя;
2. проверить membership;
3. проверить Environment;
4. проверить Runtime owner;
5. убедиться, что Runtime RUNNING;
6. при STOPPED запустить, если это разрешено;
7. дождаться readiness;
8. создать/получить Guacamole connection;
9. выдать short-lived token;
10. открыть browser session.

---

# 42. Guacamole Session Tracking

Lab Manager должен знать:

```text
user
runtime
connected_at
disconnected_at
last_activity
connection_id
```

Это используется для:

- idle detection;
- завершения Environment;
- мониторинга;
- аудита.

---

# 43. Student UI

Главный экран:

```text
Мои группы

РПРБ-21
  Linux Basic        RUNNING   [Открыть]
  Docker Lab         STOPPED
  PostgreSQL         STOPPED
```

Если Environment запущен преподавателем:

```text
[Открыть]
```

Если Runtime был auto-stopped:

```text
[Запустить и открыть]
```

Если Environment целиком не запущен:

```text
Окружение сейчас недоступно
```

Student не может самостоятельно запустить полностью остановленное Environment, если policy этого не разрешает.

---

# 44. Teacher UI

Структура:

```text
Dashboard

Groups
  -> Group
     -> Members
     -> Join Code
     -> Environments

Environments
  -> Configuration
  -> Students
  -> Demo VM
  -> Network
  -> Storage
  -> Snapshots
  -> History

Server
  -> State
  -> Capacity
```

---

# 45. Admin UI

Admin получает:

```text
Users
Teacher Permissions
Groups
Environment Profiles
Templates
Nodes
Power
Network Policies
Storage
Cleanup
Guacamole
Audit
System Health
Settings
```

---

# 46. Lab Node Agent

Agent работает на стороне физического сервера.

Он не принимает произвольные shell-команды.

Поддерживает типизированные операции:

```text
node.inventory
node.health

runtime.create
runtime.start
runtime.shutdown
runtime.stop
runtime.reboot
runtime.delete
runtime.snapshot
runtime.rollback
runtime.resize

network.create
network.attach
network.delete

storage.inventory
storage.cleanup_candidate_scan
```

---

# 47. Proxmox Adapter

Не связывать domain logic напрямую с Proxmox API.

Использовать:

```text
VirtualizationProvider
```

Реализация:

```text
ProxmoxVirtualizationProvider
```

Интерфейс должен позволять потенциально добавить другой backend без переписывания доменной модели.

---

# 48. Power Provider

Интерфейс:

```text
PowerProvider
```

Методы:

```text
power_on
power_off
power_status
```

---

# 49. Access Provider

Интерфейс:

```text
AccessProvider
```

Основная реализация:

```text
GuacamoleAccessProvider
```

Student-facing другие реализации не использовать.

Интерфейс нужен для чистой архитектуры и тестирования.

---

# 50. Network Provider

Создать abstraction:

```text
NetworkProvider
```

Он отвечает за:

```text
Environment network
isolated network
group LAN
ACL
internet access
management deny rules
```

---

# 51. Domain models

Обязательные сущности:

```text
User
TeacherProfile
StudentProfile
TeacherPermissionPolicy

Group
GroupMember

Node
PowerProviderConfig

EnvironmentProfile
Template
Environment
EnvironmentRun

Runtime
DemoRuntime

NetworkPolicy

Reservation

Snapshot

GuacamoleConnection

StoragePool
StorageUsageSnapshot
CleanupCandidate

AuditEvent
```

---

# 52. Environment model

Поля минимум:

```text
id
name
group_id
owner_teacher_id

profile_id
template_id

runtime_kind

memory_mb
swap_mb
vcpu
cpu_limit
disk_gb

network_policy_id

state

idle_timeout
max_runtime

created_at
updated_at
last_started_at
last_stopped_at
```

---

# 53. Runtime model

```text
id
environment_id
student_id
node_id

proxmox_vmid

state
ip_address

disk_ref
disk_size

last_activity_at
last_started_at
last_stopped_at

created_at
```

Уникальное ограничение:

```text
(environment_id, student_id)
```

Один Student получает только один основной Runtime внутри конкретного Environment.

---

# 54. DemoRuntime model

```text
id
environment_id
teacher_id
node_id
proxmox_vmid
state
disk_ref
created_at
```

---

# 55. TeacherPermissionPolicy

Хранить как нормализованные permissions и limits, а не только один JSON blob.

Допустим JSON для расширений, но критичные права должны быть queryable.

---

# 56. REST API — группы

```text
POST /api/groups
GET  /api/groups
GET  /api/groups/{id}
PATCH /api/groups/{id}
DELETE /api/groups/{id}

POST /api/groups/{id}/join-code/regenerate
POST /api/groups/{id}/join-code/enable
POST /api/groups/{id}/join-code/disable

POST /api/groups/join
GET  /api/groups/{id}/members
DELETE /api/groups/{id}/members/{student_id}
```

---

# 57. REST API — окружения

```text
POST /api/environments
GET  /api/environments
GET  /api/environments/{id}
PATCH /api/environments/{id}

POST /api/environments/{id}/start
POST /api/environments/{id}/stop

POST /api/environments/{id}/sync-members
POST /api/environments/{id}/snapshot

DELETE /api/environments/{id}
```

---

# 58. REST API — Runtime

```text
GET  /api/environments/{id}/runtimes

POST /api/runtimes/{id}/start
POST /api/runtimes/{id}/shutdown
POST /api/runtimes/{id}/restart
POST /api/runtimes/{id}/snapshot
POST /api/runtimes/{id}/reset

POST /api/runtimes/{id}/open
```

`open` возвращает информацию для browser-only Guacamole flow.

---

# 59. REST API — DemoRuntime

```text
GET  /api/environments/{id}/demo

POST /api/environments/{id}/demo/start
POST /api/environments/{id}/demo/stop
POST /api/environments/{id}/demo/open
POST /api/environments/{id}/demo/snapshot
POST /api/environments/{id}/demo/reset
```

---

# 60. REST API — Storage

```text
GET /api/storage
GET /api/storage/pools
GET /api/storage/usage
GET /api/storage/candidates
```

Admin cleanup:

```text
POST /api/storage/candidates/{id}/delete
POST /api/storage/candidates/{id}/archive
```

Bulk operation:

```text
POST /api/storage/cleanup
```

Каждая destructive operation требует explicit IDs.

Нельзя реализовать endpoint:

```text
DELETE /api/storage/cleanup/all
```

---

# 61. REST API — Teacher Permissions

```text
GET  /api/admin/teacher-permission-policies
POST /api/admin/teacher-permission-policies
PATCH /api/admin/teacher-permission-policies/{id}

PUT /api/admin/teachers/{id}/permission-policy
PATCH /api/admin/teachers/{id}/permissions
```

---

# 62. Реальное время

UI должен обновляться без ручного refresh.

Использовать WebSocket или SSE для:

```text
server state
Environment state
Runtime state
Guacamole connection state
capacity
storage alerts
provisioning progress
```

---

# 63. Storage cleanup safety

Перед удалением проверять dependency graph.

Например snapshot нельзя удалить, если:

```text
он требуется linked clone
```

Runtime disk нельзя удалить, если Runtime активен.

Template нельзя удалить, если от него зависят linked clones, пока storage backend требует эту зависимость.

---

# 64. Load requirements

Обязательные тесты:

```text
30 simultaneous students
30 Guacamole browser sessions
30 LXC runtime starts
30 runtime shutdowns
30 membership joins
multiple teachers starting environments concurrently
GROUP_LAN traffic
ISOLATED traffic denial
storage near threshold
```

---

# 65. Безопасность сети

Нужно тестировать:

```text
Student -> Proxmox DENY
Student -> Agent DENY
Student -> BMC DENY
Student -> VPS admin DENY

Student A -> Student B DENY in ISOLATED
Student A -> Student B ALLOW in GROUP_LAN
```

---

# 66. Reconciliation

После рестарта VPS или Agent:

1. получить Proxmox inventory;
2. получить DB state;
3. сопоставить;
4. найти orphan/missing;
5. восстановить Runtime states;
6. восстановить Environment states;
7. восстановить Guacamole registrations;
8. записать audit.

Нельзя автоматически уничтожать несопоставленные VM/LXC.

---

# 67. Server shutdown

Физический сервер можно выключать автоматически, когда:

```text
нет RUNNING Environment
нет STARTING/STOPPING Environment
нет reservations
нет активных Guacamole connections
нет Agent tasks
нет snapshots/backups
истёк idle timeout физического node
```

Перед выключением:

```text
DRAINING
```

---

# 68. Monitoring

Admin должен видеть:

```text
CPU
RAM
swap
storage
disk free
disk latency
network
temperatures if available
WireGuard
Proxmox
Agent
Guacamole
active environments
active runtimes
active browser sessions
```

---

# 69. Alerts

Минимум:

```text
storage warning
storage critical
node offline unexpectedly
Agent offline
Guacamole unavailable
failed Runtime provisioning
failed Environment start
power-on timeout
high RAM pressure
```

---

# 70. Audit

Записывать:

```text
group.create
group.join
group.member.remove

environment.create
environment.start
environment.stop
environment.delete

runtime.create
runtime.start
runtime.stop
runtime.reset
runtime.delete

demo.open

network.policy.change

storage.delete
storage.archive

teacher.permission.change

server.power_on
server.shutdown
```

---

# 71. Стек

Backend:

```text
Python 3.13+
FastAPI
SQLAlchemy 2
Pydantic
Alembic
PostgreSQL
Redis
```

Frontend:

```text
React
TypeScript
Vite
TanStack Query
React Router
```

Infrastructure:

```text
Proxmox VE
WireGuard
Apache Guacamole
Caddy or Nginx
Docker Compose on VPS
```

Node Agent:

```text
Python
systemd
mTLS / node token
```

---

# 72. Repository

```text
lab-manager/
├── apps/
│   ├── api/
│   ├── web/
│   └── node-agent/
│
├── packages/
│   ├── shared-types/
│   └── client-sdk/
│
├── infra/
│   ├── docker/
│   ├── wireguard/
│   ├── guacamole/
│   ├── systemd/
│   └── ansible/
│
├── docs/
│   ├── architecture.md
│   ├── domain.md
│   ├── networking.md
│   ├── storage.md
│   └── security.md
│
├── migrations/
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── e2e/
│   └── load/
│
├── docker-compose.yml
└── README.md
```

---

# 73. Правила разработки

Не использовать:

```text
SQLite как временную основную DB
hardcoded VMID
hardcoded student IP
root Proxmox credentials
общий student password
прямой SSH студентов
прямой RDP студентов
QR-коды
ручное создание VM как основной workflow
frontend capacity calculation
неограниченные snapshots
автоматическое удаление student data при low disk
```

---

# 74. Ключевые acceptance criteria

## Groups

- Teacher создаёт Group.
- Система генерирует code.
- Student вводит code после авторизации.
- Membership сохраняется.
- Teacher может закрыть вступление.
- QR отсутствует.

## Permissions

- Admin может запретить конкретному Teacher QEMU.
- Такой Teacher не видит QEMU profiles.
- API также блокирует попытку создать QEMU.
- Permission limits применяются backend-ом.

## Environment

- Teacher создаёт Environment для Group.
- На первом запуске создаются Runtime студентов.
- На завершении Runtime выключаются.
- Диски сохраняются.
- На следующем запуске используются те же Runtime.
- Environment можно удалить отдельно от Group.

## Demo

- Environment имеет DemoRuntime Teacher.
- Teacher открывает его через Guacamole.
- Состояние DemoRuntime сохраняется.
- DemoRuntime может участвовать в GROUP_LAN.

## Networking

- ISOLATED блокирует student-to-student.
- GROUP_LAN разрешает student-to-student внутри Environment.
- Management network недоступна в обоих режимах.

## Access

- Student может открыть Runtime только в browser.
- Прямые SSH/RDP недоступны извне.
- Runtime IP не является способом пользовательского доступа.
- Guacamole authorization привязана к конкретному Student и Runtime.

## Storage

- Admin видит занятие storage.
- Admin видит breakdown.
- Admin получает warning/critical.
- Allocation блокируется при критическом уровне.
- Cleanup показывает candidates.
- Student data не удаляется автоматически.

## Multi-teacher

- Несколько Environment разных Teacher могут работать одновременно.
- Capacity резервируется транзакционно.
- Один Teacher не может превысить свои limits.

## Persistence

- Stop/Start не уничтожает student state.
- Runtime продолжает работу с тем же disk.
- Snapshot не является обязательным для обычного сохранения состояния.

---

# 75. Основной пользовательский сценарий итогового продукта

```text
Admin
  ->
создаёт Teacher
  ->
назначает ему права:
LXC yes
QEMU no
GROUP_LAN yes
limits ...
```

Затем:

```text
Teacher
  ->
создаёт Group "РПРБ-21"
  ->
показывает code K7F3Q2
```

Студенты:

```text
login
  ->
enter K7F3Q2
  ->
membership saved
```

Преподаватель:

```text
создаёт Environment "Linux Basic"
  ->
выбирает LXC profile
  ->
ISOLATED
  ->
768 MB
```

На паре:

```text
Teacher нажимает Start Environment
  ->
Lab Manager проверяет Node
  ->
при необходимости включает server
  ->
scheduler проверяет capacity
  ->
создаются отсутствующие Runtime
  ->
создаётся DemoRuntime
  ->
Runtime запускаются
  ->
Guacamole connections готовы
```

Студент:

```text
Browser
  ->
Lab Manager
  ->
"Linux Basic"
  ->
[Открыть]
  ->
Guacamole
  ->
его Runtime
```

После пары:

```text
Teacher нажимает Stop Environment
  ->
студентам предупреждение
  ->
Guacamole sessions завершаются
  ->
Runtime graceful shutdown
  ->
disk state сохраняется
  ->
Environment STOPPED
```

Через неделю:

```text
Teacher -> Start Environment
```

Студенты получают те же машины и продолжают работу.

После завершения темы:

```text
Teacher -> Delete Environment
```

Lab Manager показывает объём данных и просит подтвердить удаление или архивирование.

---

# 76. Основной принцип

Пользователь не должен управлять виртуализацией напрямую.

Teacher думает в терминах:

```text
Group
Environment
Start
Stop
Demo
Network
Resources
```

Student думает:

```text
My Group
My Environment
Open
```

Admin думает:

```text
Permissions
Profiles
Capacity
Storage
Network
Node
Security
```

Proxmox, VMID, bridge, storage pool, internal IP, SSH credentials и Guacamole internals скрыты за Lab Manager.
