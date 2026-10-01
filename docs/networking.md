# Сеть Lab Manager

Статус: проектирование до реализации. Обязательные требования взяты из ТЗ v2.0, §§11–14, 20–21, 46, 50, 64–65. Решения, обозначенные «предложение», уточняют реализацию и не являются уже проверенной конфигурацией сервера. Текущая схема мостов прочитана с Proxmox; изоляция гостевого трафика ещё не проверена.

## Подтверждённое состояние сети узла — 30 сентября 2026

Пользователь выполнил на Proxmox read-only команды `pvesh get "/nodes/$(hostname)/network" --output-format json`, `cat /etc/network/interfaces` и `pve-firewall status`. Рабочий `vmbr0` соединён с физическим интерфейсом и несёт management-адрес и default gateway. `vmbr1` не имеет физического порта, но имеет адрес шлюза для одной общей учебной подсети. Его конфигурация содержит старые `post-up` правила NAT и фильтрации, однако это не доказывает их применения. Proxmox firewall сообщает `disabled/running`. Адреса и имена физического интерфейса установки здесь намеренно не приведены.

`vmbr1` в этом виде не обеспечивает режим `ISOLATED`: все подключённые к одному bridge гости имеют общий L2-домен, а правила IP `FORWARD` не разделяют их локальный обмен внутри bridge. Не прикреплять новые учебные Runtime к `vmbr0` или к общей untagged-сети `vmbr1` как к доказанно изолированной. `vmbr0` и действующий путь управления не менять при подготовке учебных сегментов.

Агент версии 0.8.0 подготовлен к чтению списка Linux bridges и их портов через Proxmox API. Это наблюдение помогает увидеть ошибочное подключение к физическому uplink, но не доказывает работу firewall, NAT или разделение гостей. При ошибке чтения агент сохраняет остальные показатели и сообщает `NETWORK_INVENTORY_UNAVAILABLE`; допуск машин остаётся закрытым.

01.10.2026 подтверждены Proxmox VE 9.2.20, `iptables` legacy, IPv4 forwarding включён и IPv6 forwarding выключен. Обе службы firewall имеют состояние systemd `active`, но `pve-firewall status` показывает `disabled/running`; в настройках datacenter и node нет включающих параметров, а `nft list tables` не вывел таблиц. У действующих `INPUT` и `FORWARD` политика `ACCEPT`; `POSTROUTING` не содержит NAT для учебной подсети. Это исправляет прежнее предположение о действующих `post-up` правилах: текущая конфигурация `vmbr1` не является безопасным учебным шлюзом. Результат сохранён без адресов и секретов.

Проверенные read-only команды для повторения после reboot или изменения настроек:

```bash
pveversion
iptables -V
pve-firewall status
pvesh get /cluster/firewall/options --output-format json
pvesh get "/nodes/$(hostname -s)/firewall/options" --output-format json
nft list tables
sysctl net.ipv4.ip_forward net.ipv6.conf.all.forwarding
iptables -S INPUT
iptables -S FORWARD
iptables -t nat -S POSTROUTING
ip -4 route
```

Для первого узла выбираем отдельный Linux bridge без физического uplink на каждый Runtime в режиме `ISOLATED` и один такой bridge на Environment в режиме `GROUP_LAN`. Новые имена принадлежат только Lab Manager; существующие management bridge и Tailscale остаются вне его владения. Отдельная таблица nftables Lab Manager должна по умолчанию запрещать трафик учебных сегментов, затем разрешать только нужный Guacamole-доступ и, если выбран профиль с Интернетом, выход через физический uplink после запрета инфраструктурных адресов. Проверка guest spoofing и IPv6 обязательна до запуска. Это проект реализации, не применённые правила. [Штатный legacy `pve-firewall` игнорирует VNet/FORWARD правила](https://github.com/proxmox/pve-docs/blob/master/pve-firewall.adoc), поэтому их включение не служит доказательством изоляции. До пакетного теста root-гостя и проверки после перезагрузки `NETWORK_AND_GATEWAY_NOT_VERIFIED` остаётся блокировкой admission.

01.10.2026 пользователь проверил механизм на временном Linux bridge и network namespace: ICMP к тестовому шлюзу сначала проходил, после добавления правила `nft` в отдельной `inet`-таблице ответ блокировался (`1 received` → `0 received`). Для повторения без адресов инсталляции сохранён [скрипт проверки](../tools/proxmox-network-smoke.sh). Он создаёт только объекты с тестовыми именами и удаляет их при штатном выходе; отдельная проверка отсутствия объектов после выполнения пользователем не представлена. Этот результат доказывает фильтрацию трафика к host для тестового моста, но ещё не проверяет `FORWARD`, NAT, L2 anti-spoof, Internet on/off или сохранение правил после reboot.

01.10.2026 пользователь запустил [проверку маршрутизации](../tools/proxmox-forward-smoke.sh) на Proxmox после проверки SHA-256 загруженного файла. Между двумя временными сетями ICMP сначала проходил (`1 received`, TTL 63), затем правило отдельной таблицы `inet` в hook `FORWARD` его блокировало (`0 received`). Скрипт сообщил `OK` и завершился; независимая проверка очистки после выхода не представлена. Это подтверждает конкретный IPv4-путь между временными Linux bridges, но не NAT, L2, Internet on/off и не реальные VM/LXC.

Для L2-пути подготовлен отдельный [тест bridge-forward](../tools/proxmox-bridge-smoke.sh) на двух временных namespace в одном bridge. Он ещё не выполнялся на Proxmox. До его результата и проверки защиты от подмены адресов доступ учебных машин остаётся закрытым.

Подготовлен [базовый deny-only набор nftables](../infra/proxmox/lab-network-guard.nft) для зарезервированного префикса новых мостов `lmbr`. Он ограничивает host `INPUT`, `OUTPUT`, маршрутизируемый `FORWARD`, а также L2 `input` и `forward` только для этих будущих интерфейсов, не трогая `vmbr0`, `vmbr1` или таблицы Tailscale. Это исходное состояние полного генератора policy: разрешений на Guacamole/Internet и правил против подмены адресов пока нет. Файл **не установлен** на Proxmox; публикация исходника и проверка синтаксиса в CI не являются разрешением запуска машин. Для production нужны атомарное применение полного набора правил, восстановление до autostart гостей, контроль drift и пакетные тесты.

26.09.2026 пользователь сообщил о работающем Headscale/Tailscale между VPS и Proxmox. Он заменяет предложенный ниже самостоятельно настраиваемый WireGuard-туннель. Второй туннель и настройка `PersistentKeepalive` вручную не требуются в плане этой установки. Необходимо получить адреса обоих участников, проверить правила доступа и маршрут к будущему Guacamole gateway. Подключение host Proxmox само по себе не подтверждает доступность инфраструктурной VM. Студенты не получают доступ к инфраструктурной tailnet; запрет гостям обращаться к management сохраняется.

## Инварианты

- Пользовательский доступ к Runtime и DemoRuntime — HTTPS через VPS и Guacamole.
- Студент не получает VPN, проброс портов, Proxmox Console или доступ к Agent.
- `ISOLATED` исключает связь студентов друг с другом; `GROUP_LAN` допускает её только в пределах Environment.
- Management Proxmox, BMC, storage management, Agent, управляющие адреса WireGuard и административные порты VPS недоступны гостям при любой учебной policy.
- Правила устанавливаются на доверенной стороне виртуального интерфейса и маршрутизатора. Настройки firewall внутри гостя не являются границей безопасности.
- Выданный студенту root в госте предполагает возможность менять адреса, маршруты и отправлять произвольные пакеты.
- Runtime нельзя переводить в готовое к доступу состояние до проверки фактически применённой сетевой policy.

## Целевая топология — предложение

```text
Browser
  -> HTTPS VPS: reverse proxy + Lab Manager
       -> WireGuard: узкие маршруты к инфраструктуре node
            -> отдельная инфраструктурная VM: Guacamole web + guacd
                 -> разрешённые SSH / RDP / VNC порты Runtime

Control-plane worker -> mTLS поверх WireGuard -> Lab Node Agent
Lab Node Agent -> Proxmox API / ограниченный network helper

Runtime -> host-side guest ACL -> учебный маршрутизатор -> egress policy
Runtime -X-> management / другие Environment / инфраструктурные сервисы
```

Guacamole размещается рядом с нагрузкой в отдельной инфраструктурной VM. У неё нет роли маршрутизатора между учебной и management-сетью; IP forwarding выключен. Порты guacd доступны только компоненту Guacamole web. В реализации Guacamole именно guacd устанавливает подключения к удалённым машинам. [Архитектура Apache Guacamole](https://guacamole.apache.org/doc/gug/guacamole-architecture.html).

Предлагаются отдельные зоны: `management`, `control-tunnel`, `access-gateway`, `runtime-transit`, учебные сегменты и `egress`. Их границы задаются интерфейсами и ACL, а не только разными IP-подсетями на общем bridge. Адреса и идентификаторы выделяются из конфигурации инсталляции с проверкой пересечений с LAN организации и WireGuard.

В первой целевой инсталляции один Proxmox node. Модель `NetworkSegment`, `NetworkAttachment`, `AddressLease` и версия policy сразу учитывают `node_id`; добавление второго узла потребует отдельной проверки межузловой изоляции, а не автоматического растягивания bridge.

## Обязательные режимы

| Режим | L2-домен | Связь между Runtime | DemoRuntime |
|---|---|---|---|
| `ISOLATED` | Отдельный сегмент для каждого Runtime | Запрет на L2 и маршрутизируемом L3 | Собственный сегмент |
| `GROUP_LAN` | Сегмент конкретного Environment | Разрешена в пределах сегмента по профилю | В том же сегменте либо отдельно по настройке |

Предложение базовой реализации для одного node: отдельные Linux bridge/SDN Simple VNet без физического uplink, маршрутизация через контролируемую границу на host, firewall гостевых интерфейсов и host INPUT/FORWARD. Для `ISOLATED` не полагаться только на запрет ICMP или различие подсетей. Proxmox описывает Simple VNet как локальный bridge без подключения к физическому интерфейсу. [Официальная документация SDN](https://github.com/proxmox/pve-docs/blob/master/pvesdn.adoc).

Гостевой gateway на host — только функция маршрутизации. На его guest-facing адресах запрещены все host services, кроме явно выбранных DNS/DHCP сервисов, если они вообще используются. Предпочтительный базовый профиль использует заранее выделенные адреса; DHCP для него не обязателен.

Альтернативная реализация через VLAN-aware bridge допускается внутри того же NetworkProvider только после проверки конфигурации коммутатора и access-портов. Гостям нельзя передавать management trunk или выбор VLAN. Универсальный диапазон VLAN и адресов в исходниках запрещён.

## Матрица потоков

Все разрешения ниже описывают новые соединения. Ответный трафик разрешается только для допустимых установленных соединений; односторонний ACL без проверки обратного пути недостаточен.

| Источник | Назначение | Решение |
|---|---|---|
| Internet | VPS HTTPS | Разрешить аутентифицированный прикладной путь |
| Internet | Runtime, Proxmox, Agent, Guacamole backend, guacd | Запрет; нет DNAT и публичных listeners |
| VPS proxy | Guacamole web | Только выделенный upstream через WireGuard |
| Worker | Agent | Только mTLS endpoint конкретного node |
| Agent | Proxmox API | Только необходимые операции по management-пути |
| Guacd | Выбранный Runtime | Только протокол/порт из утверждённого profile |
| Runtime | Guacamole web, guacd, Agent, Proxmox, BMC, storage | Запрет новых соединений |
| Runtime A | Runtime B, `ISOLATED` | Запрет, включая обратный путь через gateway |
| Runtime A | Runtime B, один `GROUP_LAN` | Разрешить учебный обмен |
| Environment A | Environment B | Запрет; будущие исключения только отдельной admin policy |
| Runtime | Internet | По разрешению teacher policy и egress profile |
| Runtime | DNS/NTP | Только утверждённые сервисы; не «весь LAN» |

Management deny включает реальные адреса инфраструктуры, даже если они публичные, все соответствующие IPv4/IPv6 диапазоны, LAN организации и интерфейсы самого gateway. Разрешение Internet применяется после этих запретов. Учебному трафику не выдаются маршруты к другим WireGuard peer.

## Защита при root внутри гостя

1. Интерфейс привязан к одному `runtime_id`, сегменту, разрешённому MAC и набору IP. Смена гостем конфигурации не меняет привязку.
2. Anti-spoof проверяет IP и MAC на стороне host. ARP проверяется отдельно: связь sender MAC/IP с lease, запрет подмены gateway. Одного IP firewall недостаточно для L2.
3. Теги 802.1Q/802.1ad с гостевых access-портов запрещены; проверяются вложенные теги и попытки сменить VLAN из гостя.
4. Неавторизованные DHCP server replies и IPv6 Router Advertisement запрещены. Гость не может объявить себя gateway для других участников.
5. В базовом IPv4-профиле IPv6 блокируется на host-side порту, включая link-local и multicast. Выключение IPv6 только внутри гостя не считается защитой.
6. Будущий IPv6-профиль требует эквивалентных IPv6 ACL, NDP/RA guard, anti-spoof и нагрузочных тестов. «IPv6 не настроен» не означает «IPv6 изолирован».
7. Broadcast/multicast остаются внутри учебного L2-домена; применяются ограничения потока, чтобы один Runtime не лишал остальных сети.
8. Guest-facing интерфейсы не имеют bridge-пути к физическому management uplink, storage или BMC.
9. `GROUP_LAN` допускает взаимодействие и сопутствующий риск внутри данного учебного сегмента. Запуск собственных DHCP/router лабораторий требует отдельного ограниченного профиля; граница с инфраструктурой не ослабляется.

Это требования к будущему компилятору policy и проверкам, а не заявление о наличии готовых guard-функций во всех версиях Proxmox. Proxmox документирует `ipfilter-net*`, отдельное включение firewall на vNIC и специальные правила IPv6; необходимо проверять скомпилированные правила. [Firewall Proxmox](https://github.com/proxmox/pve-docs/blob/master/pve-firewall.adoc).

## Версионная зависимость Proxmox

При проверке 2026-09-16 документация upstream `master` помечает `proxmox-firewall` на nftables как tech preview. Та же документация предупреждает: VNet/FORWARD правила не применяются stock `pve-firewall`. Поэтому нельзя назвать эти механизмы взаимозаменяемыми или гарантировать изоляцию флажком VNet. [Официальное описание firewall](https://github.com/proxmox/pve-docs/blob/master/pve-firewall.adoc).

Перед реализацией закрепить установленную версию, firewall backend и перечень поддержанных возможностей в deployment manifest. NetworkProvider выбирает поддержанный backend. Если необходимые L2/L3 ограничения не обеспечиваются штатным API, используется ограниченный типизированный network helper с отдельными правилами и явным владением ими. Нельзя параллельно изменять одни цепочки двумя контроллерами.

Production gate: изоляция подтверждена пакетными тестами после reload/reboot, правила не исчезают при обновлении Proxmox, есть независимый административный доступ для восстановления ошибочной конфигурации. Без этого guest-facing сеть не допускается к эксплуатации.

## WireGuard и сервер за NAT

Предложение: peer на физическом node либо в автоматически запускаемой инфраструктурной VM устанавливает исходящий туннель к VPS после загрузки. Когда физический сервер выключен, этот peer недоступен. `AllowedIPs` минимальны и согласованы с маршрутизацией; `0.0.0.0/0` и `::/0` не используются как удобное разрешение всего management.

Для peer за NAT при необходимости назначается `PersistentKeepalive`; официальный quickstart приводит 25 секунд как подходящий пример, а не обязательную константу для каждого peer. Это сохраняет NAT mapping, но не включает выключенный компьютер. [WireGuard Quick Start](https://www.wireguard.com/quickstart/).

Включение обеспечивается отдельным PowerProvider: BMC либо постоянно включённый LAN агент/реле. Достижимость этого устройства должна сохраняться при выключенном Proxmox. Один handshake WireGuard не переводит node в `READY`: дополнительно нужны Agent, Proxmox, storage, network и свежий inventory.

Проверяются MTU, NAT timeout, смена внешнего IP, длительные WebSocket-сеансы и восстановление туннеля после загрузки. Падение туннеля не разрешает обойти Guacamole и не освобождает вычислительные reservations без reconciliation.

## Версии policy и жизненный цикл

Предлагаемые данные: `network_policy_version`, режим, `internet_access`, `egress_profile_id`, IPv4/IPv6 strategy, `demo_attachment_mode`, segment/lease references и `applied_revision`. IP выдаётся транзакционно; его повторное использование запрещено, пока остались старые подключения или attachment.

Порядок создания: резервировать идентификаторы → создать закрытый сегмент → применить deny/anti-spoof → подключить guest vNIC → проверить → разрешить узкие access-потоки. Ошибка оставляет интерфейс закрытым, а операцию доступной для повторения.

Изменение `ISOLATED ↔ GROUP_LAN` — контролируемая операция с проверкой прав и окна остановки. Она пересоздаёт attachments/leases при необходимости, завершает прежние Guacamole-сеансы и применяет политику до запуска. Диски и Runtime identity сохраняются; новое состояние не требует клонировать все машины.

При потере control plane действующая изоляция остаётся. Agent не принимает локальные изменения от гостя; неизвестная applied revision блокирует новый доступ. Reconciliation сравнивает desired/applied/observed и помечает drift; неизвестные bridge или интерфейсы не удаляются автоматически.

## Принятая политика Internet и сегментов

DEC-05/06 закрывают OPEN-NET: система предоставляет только browser Guacamole, но root в госте с разрешённым Internet может устанавливать исходящие tunnels. Это допустимо; запрет direct inbound и management isolation сохраняется. Private IP собственной машины разрешено показывать.

TemplateVersion/ProfileVersion содержат network defaults: internet_access, ISOLATED/GROUP_LAN, named segments, allowed attachments/routes и demo attachment mode. Environment фиксирует их при создании; teacher выбирает только разрешённые Admin настройки. Для сложных лабораторий несколько сегментов внутри одного Environment, host IPAM и allowlisted ACL; произвольные management bridges/CIDR не вводятся.

Internet off блокирует IPv4/IPv6 forwarding/NAT/прокси обход через gateway; доступ guacd к машине сохраняется. Нужные внутренние DNS/NTP/учебные services перечислены отдельно. Internet on никогда не отменяет management/LAN/VPS admin deny. Разделение по сегментам не даёт доступа к другому Teacher.

## Проверки допуска реальной инсталляции

| Gate | Подтверждение |
|---|---|
| NET-01: внешняя поверхность | Внешняя проверка IPv4/IPv6 показывает только согласованные VPS endpoints; Runtime/8006/guacd недоступны |
| NET-02: infrastructure deny | Root в LXC и VM не достигает всех management-подсетей и gateway services |
| NET-03: `ISOLATED` | Два root Runtime не обмениваются unicast, broadcast, multicast, ARP/NDP, включая путь через gateway |
| NET-04: `GROUP_LAN` | Нужный обмен внутри Environment работает; между Environment закрыт; Demo obeys policy |
| NET-05: hostile guest | MAC/IP/VLAN spoof, nested VLAN, DHCP rogue, RA/NDP/ARP spoof не обходят границу |
| NET-06: restart/drift | Reboot, firewall reload, API timeout и частичный network apply не открывают временный проход |
| NET-07: NAT/power | Полный холодный старт из OFFLINE и восстановление после разрыва туннеля |
| NET-08: concurrency | 30 Runtime и 30 browser sessions; параллельное создание сетей без конфликтов адресов |
| NET-09: egress | Проверены Internet on/off по DEC-05/06, включая альтернативные IP/DNS и management через public IP |

Результаты фиксируются с версиями Proxmox/kernel/firewall, схемой интерфейсов, обезличенными counters/packet captures и датой. Пример конфигурации или mock-тест не закрывает эти gates.
