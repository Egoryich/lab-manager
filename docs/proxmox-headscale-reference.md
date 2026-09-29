# Обезличенная запись настройки Proxmox и Headscale

Источник: предоставленный пользователем `PROXMOX_HEADSCALE_DEPLOY_GUIDE.md`, прочитан полностью 28.09.2026. Ниже сохранены все 25 разделов и команды, но адреса, устройства и идентификаторы инсталляции заменены текстовыми параметрами `<...>`. Исходный файл пользователя не изменён. Пароли и ключи сюда не переносятся.

Это **историческая запись настройки стенда**, а не команды для повторного запуска на работающих серверах. Параметры `<...>` нужно заменить вручную перед использованием; они не являются shell-переменными. Основной порядок новой установки: [единый гайд](deployment-guide.md).

Уточнения при использовании записи:

- Раздел 4 содержит уничтожение данных (Wipe Disk). Он допустим только для отдельно выбранного пустого диска при первой установке. Имена `/dev/sdX` непостоянны; идентифицировать устройство по serial/by-id, размеру и текущим mounts. Существующий тестовый CT считается внешним объектом, его не удаляют и не импортируют автоматически.
- NAT/iptables в разделе 8 — конфигурация тестовой IPv4-подсети. Она не доказывает изоляцию соседей на одном bridge, IPv6, всех tailnet peers, публичных адресов инфраструктуры или отдельных окружений. Перед учебной эксплуатацией требуется сетевой этап Lab Manager и проверки обоих режимов ISOLATED/GROUP_LAN.
- `curl -k` в разделе 16 — только исторический диагностический запрос с отключённой проверкой TLS. В интеграции использовать проверяемый сертификат/CA и нужную аутентификацию; получение JSON само по себе не подтверждает авторизацию API.
- Preauth keys создаются отдельно для узлов и остаются локальными секретами. Числовые ID берутся из актуальных списков. Рекламируемый маршрут и его одобрение не заменяют ACL.
- Headscale version в командах — версия из исходной записи, не автоматический выбор latest. Конфигурацию брать для установленной версии; существующую не заменять скачанным примером при повторном запуске.
- Схема Guacamole на VPS из исходной записи не принята как целевая: Guacamole/guacd планируются на служебной VM физического сервера. VPS остаётся точкой HTTPS-входа. Lab Manager на VPS уже запущен; заключительный список исходного документа исторический.
- В двух исходных hardware-документах различались модели CPU. Присланный пользователем 29.09.2026 вывод `lscpu` подтвердил Xeon E5-2673 v3, 12 ядер / 24 потока; см. [актуальную оценку оборудования](hardware-assessment.md). Историческая запись ниже не заменяет свежий inventory.

---

# Proxmox + Headscale: гайд по развёртыванию учебного сервера

Этот файл фиксирует текущую рабочую конфигурацию учебного сервера и команды, которые использовались при развёртывании.

Текущая схема:

```text
Internet
   |
   v
VPS
├── Headscale
├── Tailscale
└── позже: Lab Manager + Guacamole
        |
        | Headscale / Tailscale
        v
Proxmox за NAT
├── Management: <PROXMOX_LAN_ADDRESS_CIDR>
├── Tailscale
├── vmbr1: <STUDENT_GATEWAY_CIDR>
└── Student Network: <STUDENT_CIDR>
       └── CT <TEST_CT_ID>: <TEST_GUEST_IP>
```

---

# 1. Исходные данные

Физический сервер:

```text
CPU: <CPU_MODEL>
RAM: <RAM_CAPACITY>

Proxmox management IP:
<PROXMOX_LAN_ADDRESS_CIDR>

Gateway:
<LAN_GATEWAY_IP>

Student network:
<STUDENT_CIDR>

Student gateway:
<STUDENT_GATEWAY_IP>

Test LXC:
CT <TEST_CT_ID>
<TEST_GUEST_IP>
```

Диски:

```text
<SYSTEM_DISK> — WD SSD 120 GB — Proxmox OS
<STUDENT_DISK> — WD HDD 500 GB — student-lvm
<RETIRED_DISK> — Hitachi 400 GB — выведен из эксплуатации
```

---

# 2. Обновление Proxmox

В веб-интерфейсе:

```text
pve
→ Updates
→ Repositories
```

Отключить:

```text
pve-enterprise
Ceph enterprise
```

Включить:

```text
pve-no-subscription
```

После этого:

```bash
apt update
apt dist-upgrade -y
```

При необходимости:

```bash
reboot
```

---

# 3. Проверка SMART дисков

Установка smartmontools:

```bash
apt update
apt install -y smartmontools
```

Проверка дисков:

```bash
smartctl -a <STUDENT_DISK>
smartctl -a <RETIRED_DISK>
smartctl -a <SYSTEM_DISK>
```

Короткий тест:

```bash
smartctl -t short <STUDENT_DISK>
smartctl -t short <RETIRED_DISK>
smartctl -t short <SYSTEM_DISK>
```

Полный тест:

```bash
smartctl -t long <STUDENT_DISK>
smartctl -t long <RETIRED_DISK>
smartctl -t long <SYSTEM_DISK>
```

После окончания теста:

```bash
smartctl -a <STUDENT_DISK>
smartctl -a <RETIRED_DISK>
smartctl -a <SYSTEM_DISK>
```

Особенно проверять:

```text
Reallocated_Sector_Ct
Current_Pending_Sector
Offline_Uncorrectable
UDMA_CRC_Error_Count
Power_On_Hours
```

---

# 4. Основной storage для VM/LXC

Используется:

```text
<STUDENT_DISK>
WD 500 GB
```

В Proxmox:

```text
pve
→ Disks
→ Disks
→ <STUDENT_DISK>
→ Wipe Disk
```

ВНИМАНИЕ: Wipe Disk полностью удаляет данные с выбранного диска.

После очистки:

```text
pve
→ Disks
→ LVM-Thin
→ Create: Thinpool
```

Параметры:

```text
Disk: <STUDENT_DISK>
Name: student-lvm
Add Storage: Yes
```

Проверить:

```text
Datacenter
→ Storage
```

Должно появиться:

```text
student-lvm
Type: LVM-Thin
Content:
- Disk image
- Container
```

---

# 5. Создание тестового LXC

Сначала скачать Debian template:

```text
pve
→ local
→ CT Templates
→ Templates
→ debian-13-standard
→ Download
```

Создать контейнер:

```text
Create CT
```

Параметры:

```text
CT ID: <TEST_CT_ID>
Hostname: test-student-01
Unprivileged container: Yes

Template:
debian-13-standard

Storage:
student-lvm

Disk:
10 GB

CPU:
2 cores

RAM:
768 MB

Swap:
256 MB
```

На первом тесте сеть можно было использовать через `vmbr0`, но итоговая конфигурация должна использовать `vmbr1`.

Установить SSH внутри контейнера:

```bash
apt update
apt install -y openssh-server
```

Проверить:

```bash
systemctl status ssh
```

---

# 6. Создание Student Network

Создать отдельный Linux Bridge:

```text
pve
→ System
→ Network
→ Create
→ Linux Bridge
```

Параметры:

```text
Name: vmbr1
IPv4/CIDR: <STUDENT_GATEWAY_CIDR>
Gateway IPv4: пусто
IPv6: пусто
Bridge ports: пусто
Autostart: Yes
VLAN aware: No
Bridge STP: No
Bridge FD: 0
MTU: 1500
Comment: Student Network
```

После создания:

```text
Apply Configuration
```

Проверить:

```bash
ip a show vmbr1
```

Должно быть:

```text
inet <STUDENT_GATEWAY_CIDR>
```

---

# 7. Включение IPv4 forwarding

Создать файл:

```bash
nano /etc/sysctl.d/99-lab-network.conf
```

Содержимое:

```text
net.ipv4.ip_forward=1
```

Применить:

```bash
sysctl --system
```

Проверить:

```bash
sysctl net.ipv4.ip_forward
```

Ожидаемый результат:

```text
net.ipv4.ip_forward = 1
```

---

# 8. NAT и изоляция Student Network

Открыть:

```bash
nano /etc/network/interfaces
```

Блок `vmbr1` должен выглядеть примерно так:

```text
auto vmbr1
iface vmbr1 inet static
        address <STUDENT_GATEWAY_CIDR>
        bridge-ports none
        bridge-stp off
        bridge-fd 0

        post-up iptables -t nat -A POSTROUTING -s '<STUDENT_CIDR>' -o vmbr0 -j MASQUERADE
        post-down iptables -t nat -D POSTROUTING -s '<STUDENT_CIDR>' -o vmbr0 -j MASQUERADE

        post-up iptables -I FORWARD -i vmbr1 -d <MANAGEMENT_CIDR> -j REJECT
        post-down iptables -D FORWARD -i vmbr1 -d <MANAGEMENT_CIDR> -j REJECT

        post-up iptables -A FORWARD -i vmbr1 -o vmbr0 -j ACCEPT
        post-up iptables -A FORWARD -i vmbr0 -o vmbr1 -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT

        post-down iptables -D FORWARD -i vmbr1 -o vmbr0 -j ACCEPT
        post-down iptables -D FORWARD -i vmbr0 -o vmbr1 -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT

        post-up iptables -I INPUT 1 -i vmbr1 -d <STUDENT_GATEWAY_IP> -p icmp -j ACCEPT
        post-up iptables -I INPUT 2 -i vmbr1 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
        post-up iptables -I INPUT 3 -i vmbr1 -m conntrack --ctstate NEW -j REJECT

        post-down iptables -D INPUT -i vmbr1 -d <STUDENT_GATEWAY_IP> -p icmp -j ACCEPT
        post-down iptables -D INPUT -i vmbr1 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
        post-down iptables -D INPUT -i vmbr1 -m conntrack --ctstate NEW -j REJECT
```

Применить без перезагрузки:

```bash
ifreload -a
```

ВАЖНО: `vmbr0` с management IP `<PROXMOX_LAN_IP>` не менять без необходимости.

---

# 9. Перенос тестового LXC в Student Network

В Proxmox:

```text
CT <TEST_CT_ID>
→ Network
→ net0
→ Edit
```

Параметры:

```text
Bridge: vmbr1

IPv4:
Static

IPv4/CIDR:
<TEST_GUEST_IP>/24

Gateway:
<STUDENT_GATEWAY_IP>
```

DNS:

```text
<DNS_RESOLVER_IP>
```

Можно задать из Proxmox Shell:

```bash
pct set <TEST_CT_ID> --nameserver <DNS_RESOLVER_IP>
```

Перезапустить:

```bash
pct reboot <TEST_CT_ID>
```

---

# 10. Проверка Student Network

Внутри CT <TEST_CT_ID>:

```bash
hostname -I
```

Ожидается:

```text
<TEST_GUEST_IP>
```

Проверки:

```bash
ping -c 4 <STUDENT_GATEWAY_IP>
ping -c 4 <DNS_RESOLVER_IP>
ping -c 4 deb.debian.org
ping -c 4 <PROXMOX_LAN_IP>
ping -c 4 <LAN_GATEWAY_IP>
```

Ожидаемый результат:

```text
<STUDENT_GATEWAY_IP>        OK
<DNS_RESOLVER_IP>          OK
deb.debian.org   OK

<PROXMOX_LAN_IP>    BLOCKED
<LAN_GATEWAY_IP>      BLOCKED
```

Проверить DNS:

```bash
cat /etc/resolv.conf
```

Ожидается:

```text
nameserver <DNS_RESOLVER_IP>
```

---

# 11. Headscale на VPS

Ниже используется домен:

```text
https://<HEADSCALE_DOMAIN>
```

Проверить архитектуру VPS:

```bash
dpkg --print-architecture
```

Установить зависимости:

```bash
apt update
apt install -y curl wget ca-certificates
```

Пример установки Headscale 0.29.4 для amd64:

```bash
cd /tmp

HEADSCALE_VERSION=0.29.4
HEADSCALE_ARCH=amd64

wget -O headscale.deb \
"https://github.com/juanfont/headscale/releases/download/v${HEADSCALE_VERSION}/headscale_${HEADSCALE_VERSION}_linux_${HEADSCALE_ARCH}.deb"

apt install -y ./headscale.deb
```

Проверить:

```bash
headscale version
```

Сделать backup конфига:

```bash
cp /etc/headscale/config.yaml /etc/headscale/config.yaml.bak
```

Скачать пример конфига той же версии:

```bash
wget -O /etc/headscale/config.yaml \
https://raw.githubusercontent.com/juanfont/headscale/v0.29.4/config-example.yaml
```

Открыть:

```bash
nano /etc/headscale/config.yaml
```

Основные параметры:

```yaml
server_url: https://<HEADSCALE_DOMAIN>

listen_addr: 127.0.0.1:8080

metrics_listen_addr: 127.0.0.1:9090

trusted_proxies:
  - 127.0.0.1/32
  - ::1/128
```

Для MagicDNS использовать отдельный base domain, например:

```yaml
base_domain: <MAGICDNS_BASE_DOMAIN>
```

Не использовать тот же hostname, что и `server_url`.

Проверить конфигурацию:

```bash
headscale configtest
```

Запустить:

```bash
systemctl enable headscale
systemctl restart headscale
systemctl status headscale --no-pager
```

Проверить локально:

```bash
curl http://127.0.0.1:8080/health
```

---

# 12. Caddy перед Headscale

Установка:

```bash
apt install -y caddy
```

Открыть:

```bash
nano /etc/caddy/Caddyfile
```

Минимальная конфигурация:

```caddy
<HEADSCALE_DOMAIN> {
    reverse_proxy 127.0.0.1:8080
}
```

Проверить:

```bash
caddy validate --config /etc/caddy/Caddyfile
```

Запустить:

```bash
systemctl enable --now caddy
systemctl reload caddy
```

Проверить:

```bash
curl https://<HEADSCALE_DOMAIN>/health
```

---

# 13. Создание пользователя Headscale

На VPS:

```bash
headscale users create infra
```

Проверить:

```bash
headscale users list
```

Создать preauth key:

```bash
headscale preauthkeys create --user <HEADSCALE_USER_ID>
```

Ключ является секретом.

---

# 14. Установка Tailscale на VPS

```bash
curl -fsSL https://tailscale.com/install.sh | sh
```

Подключение к собственному Headscale:

```bash
tailscale up \
  --login-server=https://<HEADSCALE_DOMAIN> \
  --authkey=<PREAUTH_KEY>
```

Проверить:

```bash
tailscale status
tailscale ip -4
tailscale debug prefs
```

---

# 15. Установка Tailscale на Proxmox

На VPS создать отдельный ключ:

```bash
headscale preauthkeys create --user <HEADSCALE_USER_ID>
```

На Proxmox:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
```

Подключить:

```bash
tailscale up \
  --login-server=https://<HEADSCALE_DOMAIN> \
  --authkey=<SECOND_PREAUTH_KEY>
```

Проверить:

```bash
tailscale status
tailscale ip -4
tailscale debug prefs
```

В `tailscale debug prefs` должно быть:

```text
ControlURL: https://<HEADSCALE_DOMAIN>
WantRunning: true
LoggedOut: false
```

---

# 16. Проверка туннеля VPS ↔ Proxmox

На обеих машинах:

```bash
tailscale status
```

С VPS:

```bash
ping -c 4 <PROXMOX_TAILNET_IP>
```

С Proxmox:

```bash
ping -c 4 <VPS_TAILNET_IP>
```

Проверка Proxmox API с VPS:

```bash
curl -k https://<PROXMOX_TAILNET_IP>:8006/api2/json/version
```

Если приходит JSON с версией Proxmox, туннель работает.

---

# 17. Публикация Student Network через Proxmox

На Proxmox:

```bash
tailscale set --advertise-routes=<STUDENT_CIDR>
```

Проверить:

```bash
tailscale debug prefs
```

Должно появиться:

```text
AdvertiseRoutes:
<STUDENT_CIDR>
```

---

# 18. Разрешение маршрута в Headscale

На VPS:

```bash
headscale nodes list-routes
```

Должен появиться маршрут:

```text
<STUDENT_CIDR>
```

Одобрить:

```bash
headscale nodes approve-routes \
  --identifier <PROXMOX_NODE_ID> \
  --routes <STUDENT_CIDR>
```

Здесь `<PROXMOX_NODE_ID>` — ID узла Proxmox. Перед выполнением всегда проверять:

```bash
headscale nodes list-routes
```

После одобрения должно быть примерно:

```text
Approved:  <STUDENT_CIDR>
Available: <STUDENT_CIDR>
Serving:   <STUDENT_CIDR>
```

---

# 19. Принятие subnet routes на VPS

На VPS:

```bash
tailscale set --accept-routes=true
```

Проверить:

```bash
tailscale debug prefs
```

Затем:

```bash
ping -c 4 <STUDENT_GATEWAY_IP>
ping -c 4 <TEST_GUEST_IP>
```

Рабочий результат:

```text
VPS
  |
Headscale / Tailscale
  |
Proxmox
  |
vmbr1
  |
<TEST_GUEST_IP>
```

---

# 20. Текущий рабочий статус

На текущем развёртывании подтверждено:

```text
Headscale tunnel работает

Proxmox рекламирует:
<STUDENT_CIDR>

Headscale:
Approved  = <STUDENT_CIDR>
Available = <STUDENT_CIDR>
Serving   = <STUDENT_CIDR>

VPS → <TEST_GUEST_IP>:
PING OK
0% packet loss
```

---

# 21. Полезные диагностические команды

## Proxmox

Сеть:

```bash
ip a
ip route
ip a show vmbr0
ip a show vmbr1
```

IP forwarding:

```bash
sysctl net.ipv4.ip_forward
```

iptables:

```bash
iptables -L -n -v
iptables -t nat -L -n -v
```

LXC:

```bash
pct list
pct status <TEST_CT_ID>
pct config <TEST_CT_ID>
```

Запуск:

```bash
pct start <TEST_CT_ID>
```

Остановка:

```bash
pct shutdown <TEST_CT_ID>
```

Принудительная остановка:

```bash
pct stop <TEST_CT_ID>
```

Перезапуск:

```bash
pct reboot <TEST_CT_ID>
```

---

## Tailscale

```bash
tailscale status
tailscale ip -4
tailscale debug prefs
```

Изменить рекламируемые маршруты:

```bash
tailscale set --advertise-routes=<STUDENT_CIDR>
```

Принять маршруты:

```bash
tailscale set --accept-routes=true
```

---

## Headscale

Пользователи:

```bash
headscale users list
```

Узлы:

```bash
headscale nodes list
```

Маршруты:

```bash
headscale nodes list-routes
```

Одобрение маршрута:

```bash
headscale nodes approve-routes \
  --identifier NODE_ID \
  --routes <STUDENT_CIDR>
```

Preauth keys:

```bash
headscale preauthkeys create --user <HEADSCALE_USER_ID>
```

---

# 22. Проверка после перезагрузки Proxmox

После reboot физического сервера проверить:

```bash
ip a show vmbr1
sysctl net.ipv4.ip_forward
tailscale status
tailscale debug prefs
```

Затем с VPS:

```bash
headscale nodes list-routes
ping -c 4 <TEST_GUEST_IP>
```

Маршрут должен снова быть:

```text
Approved
Available
Serving
```

---

# 23. Что пока не развёрнуто

Следующие части являются дальнейшими этапами:

```text
Apache Guacamole
Lab Manager
Node Agent
автоматическое включение сервера
гибкая сетевой режим ISOLATED/GROUP_LAN
автоматическое создание LXC/VM
демонстрационная VM преподавателя
idle shutdown
storage cleanup UI
```

Текущий сетевой фундамент уже готов для следующего этапа:

```text
Browser
   ↓
VPS
   ↓
Guacamole
   ↓ Headscale/Tailscale
Proxmox
   ↓
Student Network
   ↓
LXC / VM
```

---

# 24. Важные адреса текущей схемы

```text
Proxmox LAN:
<PROXMOX_LAN_IP>

Local gateway:
<LAN_GATEWAY_IP>

Student gateway:
<STUDENT_GATEWAY_IP>

Student subnet:
<STUDENT_CIDR>

Test LXC:
<TEST_GUEST_IP>

Headscale:
https://<HEADSCALE_DOMAIN>
```

---

# 25. Важные правила

1. Не открывать Proxmox `:8006` напрямую в Интернет.
2. Не выдавать студентам прямой SSH/RDP/VPN.
3. Management LAN `<MANAGEMENT_CIDR>` должна быть закрыта для Student Network.
4. Студенческий доступ в дальнейшем идёт только через Apache Guacamole.
5. `<RETIRED_DISK>` Hitachi больше не использовать.
6. Перед удалением/форматированием диска всегда проверять имя устройства через:

```bash
lsblk
```

7. Перед изменением `/etc/network/interfaces` сохранять копию:

```bash
cp /etc/network/interfaces /etc/network/interfaces.bak
```

8. После изменения сети использовать:

```bash
ifreload -a
```

вместо немедленной перезагрузки, чтобы сначала проверить доступность Proxmox.
