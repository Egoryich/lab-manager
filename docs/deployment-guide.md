# Единый гайд развёртывания Lab Manager

Точка входа для повторной установки с нуля. Реальные домены, IP, номера устройств и секреты остаются в локальном inventory и серверных конфигурациях. Здесь используются параметры и зарезервированный пример `example.org`. Loopback и номера портов обозначают технические интерфейсы сервисов.

Правило ведения гайда: после подтверждённого успешного выполнения сохранять команды в соответствующем разделе вместе с предусловиями, местом запуска (VPS/Proxmox/локально), ожидаемым результатом, датой и источником проверки. Успех со слов пользователя отмечать отдельно от локального/CI-теста. Адреса и секреты заменять параметрами; непроверенные шаги не помечать выполненными. Исправления заменяют основной рекомендуемый способ, а причина отказа остаётся в диагностике.

Проверен первый VPS-запуск с HTTPS и входом Admin. Agent, Guacamole и управление учебными машинами ещё разрабатываются: туннель и работающий сайт не означают готовность всей системы.

## 1. Параметры

| Параметр | Назначение |
|---|---|
| `LAB_DOMAIN` | Отдельный hostname Lab Manager |
| `HEADSCALE_DOMAIN`, `MAGICDNS_BASE_DOMAIN` | Разные имена сервера Headscale и DNS-зоны tailnet |
| `VPS_PUBLIC_IP`, `VPS_TAILNET_IP`, `PROXMOX_TAILNET_IP` | Внешний и служебные адреса |
| `MANAGEMENT_CIDR`, `PROXMOX_LAN_IP`, `LAN_GATEWAY_IP` | Управляющая сеть физического сервера |
| `STUDENT_CIDR`, `STUDENT_GATEWAY_CIDR`, `TEST_GUEST_IP` | Тестовая гостевая подсеть |
| `SYSTEM_DISK`, `STUDENT_DISK`, `RETIRED_DISK` | Диски, определённые по serial/by-id и назначению |
| `TEST_CT_ID`, `HEADSCALE_USER_ID`, `PROXMOX_NODE_ID` | ID из актуального inventory/CLI |
| `VERIFIED_COMMIT_SHA`, `VERIFIED_UPDATER_SHA` | Полные SHA с успешным CI и публикацией обоих образов |

Для shell-команд задайте свой домен в каждом новом терминале:

```bash
LAB_DOMAIN='lab.example.org' # заменить собственным hostname
```

Ключи регистрации, PostgreSQL passwords, encryption/digest keys, registry/SSH credentials в гайд не записываются. Существующие файлы секретов сохраняются при обновлении.

## 2. Физический сервер и сеть

Сначала собрать факты без изменения дисков и сети:

```bash
lscpu
free -h
lsblk -o NAME,SIZE,MODEL,SERIAL,FSTYPE,MOUNTPOINTS
pveversion -v
pvesm status
lvs -o vg_name,lv_name,lv_size,data_percent,metadata_percent
pct list
qm list
ip -br address
ip route
```

Подтверждено пользователем 29.09.2026: на Proxmox успешно выполнены `lscpu`, `free -h`, `pveversion -v`, `pvesm status`, `lvs -o vg_name,lv_name,lv_size,data_percent,metadata_percent`, `pct list`, `qm list`. Предусловия: установленный Proxmox и root-shell; команды только читают состояние. Ожидаемый результат — характеристики CPU/RAM, версии, активные storage, заполнение thin data/metadata и списки гостей; пустой `qm list` допустим при отсутствии QEMU VM. В присланном выводе два остановленных тестовых LXC и ни одной QEMU VM. Результаты учтены в [оценке оборудования](hardware-assessment.md); остальные команды этого блока данным сообщением не проверены. Тестовое назначение гостей не разрешает их автоматическое удаление.

Все 25 разделов предоставленного гайда сохранены в [обезличенной записи Proxmox + Headscale](proxmox-headscale-reference.md): repositories, SMART, storage, тестовый LXC, bridge, forwarding/NAT, Headscale, Caddy, Tailscale, маршруты и проверки после reboot. Адреса и ID заменены текстовыми параметрами `<...>`, которые нужно заменить перед выполнением. Это историческая запись стенда, а не скрипт для повторного применения к работающему серверу. Wipe Disk относится только к выбранному пустому диску при первой установке.

Тестовый NAT не доказывает ISOLATED/GROUP_LAN, IPv6 и защиту всей инфраструктуры. Маршрут также требует policy/ACL. Целевые Guacamole/guacd размещаются на служебной VM физического сервера, а VPS принимает браузерный HTTPS. `curl -k` из исторической записи не используется в production-клиенте.

Команды Headscale сверены с [установкой официального пакета](https://headscale.net/stable/setup/install/official/) и [управлением маршрутами](https://headscale.net/stable/ref/routes/). Конфигурацию выбирают для установленной версии; существующую не заменяют скачанным примером. Для повторной регистрации не используют прежние preauth keys.

## 3. VPS и DNS

```bash
cat /etc/os-release
dpkg --print-architecture
free -h
df -h /
sudo ss -lntup
systemctl --no-pager --type=service --state=running
getent ahostsv4 "${LAB_DOMAIN:?Set LAB_DOMAIN first}"
getent ahostsv6 "$LAB_DOMAIN"
```

Если есть A/AAAA, обе записи должны вести на доступный VPS. Headscale и Tailscale устанавливаются по разделам 11–19 записи стенда с собственными параметрами. Проверить на обеих машинах `tailscale status` и `tailscale ip -4`; на VPS — `headscale version`, `sudo headscale configtest`, `sudo headscale nodes list-routes`, `systemctl is-active caddy headscale tailscaled`.

## 4. Docker Engine на Ubuntu VPS

Для новой установки используйте официальный apt-репозиторий. Codename берётся из ОС; существующий Docker повторно не переустанавливается. [Инструкция Docker](https://docs.docker.com/engine/install/ubuntu/).

```bash
sudo apt update
sudo apt install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo docker run --rm hello-world
sudo docker compose version
```

## 5. Приложение, Admin, HTTPS

Следовать [первому VPS-запуску](vps-first-install.md): успешный SHA → checkout → `.env.vps` → pull → PostgreSQL/Redis → миграции → API/web → локальная готовность → bootstrap-admin → блок существующего Caddy → публичная готовность. Сборка образов выполняется в GitHub, не на VPS. Headscale и действующий Caddy сохраняются.

## 6. Автообновление и диагностика

Следовать [инструкции updater](vps-auto-update.md). Установщик сначала сохраняется в файл и запускается из него. Перед первым bootstrap updater checkout не переключают. Если приложение работает, но timer отсутствует, не пересоздавайте базу:

```bash
systemctl show lab-manager-update.timer -p LoadState -p ActiveState -p UnitFileState
systemctl list-timers --all lab-manager-update.timer --no-pager
systemctl show lab-manager-update.service -p LoadState -p Result -p ExecMainStatus
sudo journalctl -u lab-manager-update.service -n 40 --no-pager
sudo test -f /usr/local/lib/lab-manager/update-vps.py && echo updater-file-present
```

`LoadState=not-found` означает отсутствие загруженного unit. Неактивный timer не выполняет опрос; неактивный завершившийся oneshot service допустим. Пустой journal не показывает ошибку shell-установщика — нужен его собственный вывод. Не присылайте `.env.vps`, `previous.env`, `candidate.env` или полный `docker compose config`.

## 7. Проверки после запуска и reboot

```bash
curl --fail "https://${LAB_DOMAIN:?Set LAB_DOMAIN first}/api/health/ready"
systemctl is-active caddy headscale tailscaled
cd /opt/lab-manager/repo
sudo docker compose --env-file .env.vps -f infra/vps/compose.yml ps
sudo docker stats --no-stream
free -h
df -h /
systemctl list-timers --all lab-manager-update.timer --no-pager
```

Отдельно проверить вход Admin и восстановление туннеля после включения Proxmox. Остальные этапы полной поставки перечислены в [плане реализации](implementation-plan.md).

## 8. Следующий выпуск: очередь и worker

Для добавления пятого контейнера и миграции `0003_operations` используйте [пошаговый переход](vps-worker-rollout.md). Не применять как обычное автоматическое обновление: требуется окно остановки API, проверка схемы, готовности worker и новый baseline updater. Выполнение этого перехода на VPS пока не подтверждено пользователем.
