# Развёртывание с чистого сервера, VPS и домена

Это план установки целевой системы и проверяемый состав будущей поставки. Инфраструктурные шаги ниже можно подготовить заранее. Сам Lab Manager ещё не реализован: сейчас нет release images, installer, миграций или готового Compose, поэтому команды запуска продукта не выдумываются.

Требования пользователя: чистый физический сервер, чистая VPS, существующий домен, включение через Tuya, самостоятельная регистрация, backup-функция Lab Manager отключена. Источник уточнений: [decisions.md](decisions.md).

## 1. Что где будет работать

| Место | Компоненты | Когда работает |
|---|---|---|
| VPS | Reverse proxy/TLS, Lab Manager API/UI, worker/scheduler, PostgreSQL, Redis, Tuya client | Постоянно |
| Физический сервер | Proxmox VE, WireGuard peer, Lab Node Agent с systemd | Пока сервер включён |
| Инфраструктурная VM на Proxmox | Guacamole web + guacd + проверенное расширение Lab Manager | Автоматически после загрузки Proxmox, до учебных машин |
| Учебные гости | LXC и QEMU, по одному на студента и Demo | По управляемому запуску/возобновлению окружения |
| Tuya-плата и домашняя сеть | Независимый путь включения | Должны оставаться доступными при выключенном сервере |

Пользователь открывает один адрес вида `https://lab.<ваш-домен>`. API/UI и Guacamole browser traffic доступны через этот origin; Guacamole backend, Proxmox и Agent не выставляются студентам в Интернет. VPS не обязана хранить учебные диски.

## 2. Какие данные нужно заполнить

Создать локальную копию [infrastructure.example.json](infrastructure.example.json): CPU/RAM/диски, адрес VPS, домен/поддомен, LAN и management подсети, Proxmox storage, модель Tuya. Реальные токены и private keys хранятся отдельно от inventory и Git.

Проверяемые условия: виртуализация CPU включена; известны диски для установки; у node есть стабильный LAN адрес; роутер и Tuya питаются при выключенном сервере; доступен административный канал к VPS и физической консоли/локальной сети сервера. Без характеристик нельзя выбрать размеры Guacamole VM, guests, disk reserve и подтвердить 30 студентов.

Для VPS и инфраструктурной VM предлагается Ubuntu Server 24.04 LTS как единый baseline. Это поддерживаемая Docker ОС; точные пакеты/образы будут закреплены release manifest. [Docker: поддерживаемые Ubuntu](https://docs.docker.com/engine/install/ubuntu/). Версия Proxmox и тип storage выбираются после получения характеристик и compatibility test.

## 3. Установить Proxmox на физический сервер

Скачать официальный установочный ISO, сверить опубликованную контрольную сумму, записать установочный носитель. В installer выбрать нужный диск, hostname, management IP, gateway, DNS и учётную запись администратора. Разметка затрагивает выбранные диски; конкретные device names заранее не назначаются. [Официальная инструкция установки Proxmox](https://github.com/proxmox/pve-docs/blob/master/pve-installation.adoc).

После установки зайти в Proxmox из административной LAN, применить обновления из выбранного официального репозитория, проверить время/NTP, storage и сетевые интерфейсы. Не использовать management bridge как учебную общую сеть. Организовать доступ для обслуживания, который не зависит от работающего Lab Manager.

Команды инвентаризации на установленном Proxmox, без изменения конфигурации:

```sh
pveversion -v
lscpu
free -h
lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINTS
pvesm status
ip -brief address
```

Итог шага: известны pool capacities/capabilities, физические ресурсы и интерфейсы. LVM-thin/ZFS не выбираются только по примеру из документа; разметка и overhead зависят от дисков и памяти.

## 4. Подготовить VPS и домен

Установить выбранную поддерживаемую ОС, обновить систему, настроить время и отдельного администратора с SSH-ключом. Проверить новый вход до изменения правил удалённого администрирования. SSH на VPS — средство оператора, не способ доступа студента к Runtime.

DNS: A-запись `lab.<домен>` указывает на публичный IPv4 VPS. AAAA добавляется только при действительно настроенном IPv6 и эквивалентном firewall. Проверять разрешение имени с внешнего устройства.

| Порт/служба VPS | Кто использует |
|---|---|
| TCP 443 | Браузеры пользователей, HTTPS/WebSocket |
| TCP 80 | Redirect/проверка сертификата при выбранном способе ACME |
| UDP WireGuard, например 51820 | Только инфраструктурный peer node, фактический порт в inventory |
| Административный SSH | Только разрешённый административный путь |
| PostgreSQL, Redis, Agent, guacd, Proxmox 8006 | Не публикуются как пользовательские Интернет endpoints |

Предлагаемый reverse proxy — Caddy. Автоматический TLS требует корректного DNS и доступности выбранного ACME challenge; для обычного HTTP/TLS challenge используются соответствующие публичные порты. [Caddy automatic HTTPS](https://caddyserver.com/docs/automatic-https).

## 5. Установить Docker Engine и Compose на VPS

Использовать официальный Docker apt repository и Compose plugin по [инструкции Docker](https://docs.docker.com/engine/install/ubuntu/). Закрепить версии, которые будут проверены для релиза. Не требуется Docker Desktop на сервере.

После установки проверить:

```sh
sudo systemctl is-active docker
sudo docker version
sudo docker compose version
```

Для production Compose нужны постоянные volumes PostgreSQL/прокси, health checks, restart policy, выделенные внутренние сети и выдача secret files только нужным сервисам. Docker-published ports проверяются отдельно: обычное правило UFW само по себе не доказывает запрет доступа к опубликованному контейнерному порту. Это описано в [официальной инструкции](https://docs.docker.com/engine/install/ubuntu/).

## 6. Настроить WireGuard между node и VPS

VPS — доступная публичная точка. Node за NAT сам устанавливает исходящий туннель; peer на Proxmox стартует независимо от учебных гостей. Создать отдельные ключи, небольшой непересекающийся туннельный диапазон и минимальные AllowedIPs. Значения адресов и ключей появляются после inventory.

При необходимости NAT mapping поддерживается PersistentKeepalive. Это помогает доставке по существующему каналу, но не включает выключенный node. [WireGuard Quick Start](https://www.wireguard.com/quickstart/).

Проверки: handshake, обмен между нужными инфраструктурными endpoints, отсутствие маршрута из student network к management, восстановление после reboot и смены внешнего адреса. Учебные гости не получают WireGuard keys или peer.

## 7. Подготовить инфраструктурную VM и учебные сети

В Proxmox создать инфраструктурную VM из проверенного Linux image. Зарезервировать под неё RAM/CPU/disk отдельно от студентов. На этапе установки администратор создаёт эту инфраструктуру; дальнейшие student guests создаёт Agent автоматически.

VM запускается средствами Proxmox до учебных машин, даже если Lab Manager пока ждёт READY. Здесь будет Guacamole web, guacd и совместимое auth/assistance расширение. У VM нет права управлять Proxmox и нет открытого маршрутизатора в management. Guacd устанавливает только разрешённые guest SSH/RDP/VNC connections.

Учебные сети создаются NetworkProvider после регистрации node. Минимум: ISOLATED и GROUP_LAN; template/profile задаёт Internet on/off. Для разбиения окружения на несколько учебных сегментов нужны named segments, attachments и явно разрешённые маршруты. Все адреса выделяются из проверенного student CIDR, не из management/LAN организации.

Перед первым учебным запуском выполнить [NET gates](networking.md): проверки из root-гостя, IPv4/IPv6, запрета management, student isolation и разделения Environment. Наличие разных IP само по себе не заменяет эти проверки.

## 8. Подключить Tuya

Подключить Tuya Cloud Central Europe к VPS с отдельным secret. Пользователь предоставил PCIe PWR/RESET плату и switch_1/ModeReset commands; сверить их functions и получить реальный status payload. Не трактовать board online или command ACK как питание ПК и не выполнять повторный Power после timeout без проверки.

Итог: выключенный node включается из VPS; потеря ответа не приводит к повторному нажатию Power; успешная команда Tuya не считается READY. Полная процедура и ограничения: [power-tuya.md](power-tuya.md).

## 9. Что должен предоставить релиз Lab Manager

Этот этап сейчас **не исполняется**, потому что продукт ещё разрабатывается. Критерии поставки:

1. Release manifest с версиями API/worker/web/agent/Guacamole extension, Python/JDK, PostgreSQL/Redis и image digests.
2. Dev и production Compose; Ansible roles для VPS/node/gateway, systemd units и inventory example. Настоящий root `docker-compose.yml` с понятным назначением.
3. Проверка конфигурации до запуска: DNS, storage, network overlaps, power provider, secrets, disable-backups, совместимость выбранной persistence policy.
4. Миграции PostgreSQL и одноразовая интерактивная инициализация первого Admin без стандартного пароля.
5. Выпуск node identity/mTLS, установка Agent и ограниченного network helper, autostart и каталог durable command journal.
6. Создание сервисного Proxmox API token с проверенными минимальными ACL, вместо root credentials; регистрация node и inventory.
7. Установка gateway extension/broker, проверка tunnel авторизации, revoke, совместного просмотра и предупреждений перед завершением пары.
8. Подключение reverse proxy к реально существующим API/UI/access services; TLS и внешняя проверка published ports.
9. Runbook установки, повторного запуска, обновления и диагностики, проверенный на чистых машинах. Команды в нём должны соответствовать файлам поставки, а не именам из проектного примера.

## 10. Создать учебные templates и профили

Подготовить хотя бы один unprivileged LXC Linux template и один QEMU template для сценариев, требующих VM. Если Windows нужен — отдельный лицензированный template. Включить поддержанные guest readiness/shutdown средства, уникальные credentials, SSH/RDP/VNC по профилю.

TemplateVersion фиксирует image digest, kind, протокол и network defaults. Profile задаёт RAM/vCPU/CPU credits/disk, demo profile, Internet on/off, изоляцию/сегменты и согласованное завершение пары. Нельзя клонировать общий student password или менять image существующего Environment при обновлении каталога.

VM hibernation выбрана пользователем: отдельно проверить storage для state file, запас места, resume после reboot host, guest time/network reconnection. Это не backup, а рабочая часть persistence. LXC по согласованной policy выключается с сохранением файлов, без сохранения процессов.

## 11. Первичная настройка через UI

Admin создаёт Teacher, назначает разрешённые student profiles, обязательный доступный demo profile, квоты и сеть. Student регистрируется сам и вступает по коду. Восстановление аккаунта выполняет Admin одноразовой процедурой; SMTP не обязателен.

Teacher создаёт Group и Environment, бронирует ресурс на время занятия и запускает его. У Teacher нет чужих групп/окружений. Проверить подключение студента и помощь Teacher в той же сессии, если compatibility test пройден.

Различать три действия: завершить пару с сохранением; архивировать с сохранением на том же storage; удалить Environment с данными. Исключение Student — отдельное разрушительное действие только для его данных в этой Group. Backup jobs Lab Manager выключены; snapshots доступны только Admin, Teacher/Student не могут включить их через override.

## 12. Проверить полный цикл до реального занятия

На тестовых данных: выключенный node → Tuya boot → READY → start/resume → browser session → помощь Teacher → конец пары → подтверждённое освобождение compute → выключение node → следующий boot/start → прежние данные. Для QEMU — также прежние процессы из сохранённой RAM; для LXC — прежние файлы.

Отдельно: два Teacher одновременно бронируют последний ресурс; нет чужих данных в API/UI; no Internet профиль действительно закрывает egress; архив не освобождает disk; kick удаляет только целевые данные; critical 10% блокирует новое выделение; сбой Tuya/Agent/storage не создаёт ложный успех.

После небольшого теста — обязательная нагрузка 30 студентов плюс Demo и запас на инфраструктуру. Без характеристик сервера и результатов этой проверки нельзя обещать готовность указанной вместимости.

## Что можно сделать до реализации продукта

Сообщить характеристики и модель Tuya, выбрать доменный адрес, установить Proxmox и ОС VPS, собрать inventory. Полную конфигурацию сетей/дисков и команды развёртывания Lab Manager завершать после утверждения конкретного оборудования и появления проверенной поставки. Эта инструкция показывает весь путь и чётко отделяет существующие средства от будущих артефактов проекта.
