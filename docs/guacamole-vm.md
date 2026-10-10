# Подготовка отдельной Guacamole VM

Скрипт [prepare-guacamole-vm.sh](../tools/prepare-guacamole-vm.sh) создаёт выключенную Debian cloud VM на служебном storage Proxmox. Он не изменяет маршруты или firewall, не запускает VM и не устанавливает Guacamole. Это первый шаг до настройки выделенного сетевого выхода, Headscale и Guacamole Compose. Студенческие LXC остаются на `student-lvm`.

Перед запуском администратор проверяет, что VMID и адрес VM не заняты, bridge принадлежит закрытой инфраструктурной сети, а выбранный storage содержит не менее 20 GiB свободного места. Пример без адресов инсталляции:

```bash
bash tools/prepare-guacamole-vm.sh \
  '<FREE_VMID_OUTSIDE_STUDENT_RANGE>' \
  '<INFRA_STORAGE>' \
  '<INFRA_BRIDGE>' \
  '<VM_IPV4/24>' \
  '<BRIDGE_GATEWAY_IPV4>'
```

Скрипт скачивает официальный Debian 13 generic cloud image и `SHA512SUMS` по HTTPS, проверяет сумму, создаёт диск 16 GiB, 2 vCPU и 2 GiB RAM. Он создаёт отдельный SSH-ключ root Proxmox для первоначального администрирования VM; приватный ключ не выводится. Cloud-init задаёт статический IPv4, шлюз, пользователя `labadmin` и публичный ключ. VM остаётся выключенной с `onboot=0` до проверки сетевого выхода. При ошибке скрипт оставляет частично созданный VMID для осмотра и ничего автоматически не удаляет.

Перед первым запуском [установщик сетевых правил](../tools/install-guacamole-egress.sh) берёт IP и MAC из конфигурации выключенной VM, сверяет выделенный bridge, проверяет синтаксис nftables и устанавливает [отдельную systemd-службу](../infra/proxmox/lab-guacamole-egress.service). [Генератор](../tools/render-guacamole-egress.py) разрешает VM выход к учебным машинам только по SSH и обычный выход в Интернет, закрывает инфраструктурные адреса и доступ других узлов к VM через этот bridge. Администратор Proxmox может подключиться к VM по SSH для первоначальной настройки. VM и firewall Proxmox в других сетях не затрагиваются. Пример без адресов инсталляции:

```bash
bash tools/install-guacamole-egress.sh \
  '<GUACAMOLE_VMID>' '<DEDICATED_BRIDGE>' '<PUBLIC_UPLINK_BRIDGE>' \
  '<LAB_ADDRESS_POOL>' \
  tools/render-guacamole-egress.py \
  infra/proxmox/lab-guacamole-egress.service
```

Установщик требует пустой bridge, работающий базовый защитный набор Lab Manager, IPv4 forwarding и выключенную VM с `onboot=0`. Он сохраняет только собственные таблицы nftables. **Политика учебных сегментов пока deny-only**, поэтому её отдельная настройка нужна до подключения Guacamole к LXC. После первого успешного запуска надо проверить `qm config`, доступность VM из Proxmox, отсутствие прямого доступа студентов и доступ Guacamole только через VPS. Затем отдельно включить `onboot=1` и сохранить реально выполненные команды и результаты в этом гайде. До этих проверок это подготовка, а не подтверждённый production gateway.

Если тестовая VM уже работает, [обёртка](../tools/activate-guacamole-egress.sh) принимает те же VMID, bridge и диапазон лаборатории без путей к трём файлам. Она проверяет имя VM и аргументы **до** остановки, загружает файлы из закреплённого коммита и сверяет их SHA-256, затем останавливает только эту VM, вызывает установщик и запускает её снова. При ошибке после остановки VM остаётся выключенной для проверки; повторять установку вслепую нельзя. Выполнение этой обёртки на стенде пока не подтверждено.

```bash
bash tools/activate-guacamole-egress.sh \
  '<GUACAMOLE_VMID>' '<DEDICATED_BRIDGE>' '<PUBLIC_UPLINK_BRIDGE>' \
  '<LAB_ADDRESS_POOL>'
```

Источники: [Debian cloud images](https://cloud.debian.org/images/cloud/trixie/latest/), [Proxmox Cloud-Init Support](https://pve.proxmox.com/wiki/Cloud-Init_Support).

### Проверка на стенде 10 октября

Обёртка из закреплённого выпуска прошла проверку SHA-256 и установила `lab-guacamole-egress.service` и файл правил. После её завершения служба была `active`, а выделенная Guacamole VM осталась `stopped`; итогового сообщения `PASS` от обёртки не было. Причина остановки финального запуска не установлена, поэтому результат обёртки нельзя считать полным успехом и повторять её поверх уже созданных файлов нельзя.

После отдельной проверки имени VM, состояния службы и трёх таблиц nftables администратор запустил только Guacamole VM командой `qm start <GUACAMOLE_VMID>`; `qm status` вернул `running`. С VPS частный URL Guacamole ответил HTTP 200. Проверенная последовательность восстановления без адресов и секретов:

```bash
vmid='<GUACAMOLE_VMID>'; test "$(qm config "$vmid" | sed -n 's/^name: //p')" = lab-guacamole && systemctl is-active --quiet lab-guacamole-egress.service && nft list table inet lab_guac_filter >/dev/null && nft list table ip lab_guac_nat >/dev/null && nft list table bridge lab_guac_l2 >/dev/null && test "$(qm status "$vmid")" = 'status: stopped' && qm start "$vmid" && qm status "$vmid"
```

Ответ HTTP 200 подтверждает доступность шлюза, но не доказывает браузерный вход студента или SSH-доступ к LXC. Эти проверки выполняются в сквозном сценарии занятия.

В тот же день pull-updater Proxmox применил выпуск агента `5709d1baa8c4ecfcb3ac8d319a5d9487bc21c1fa`: `Result=success`, `ExecMainStatus=0`, службы агента, помощника сегментов, базовой защиты и защиты Guacamole — `active`. Закреплённый скрипт `configure-guacamole-source.sh` прошёл проверку SHA-256 и сохранил IP и bridge работающей Guacamole VM в root-owned конфигурации. Проверка через непривилегированного клиента помощника вернула `ready: true`. VPS принял следующий снимок: `admission_ready=true`, `network_security.ready=true`, `error_code=OK`. Это подтверждает готовность инфраструктуры к размещению, но не запуск LXC или браузерный доступ студента.

Команды для проверки следующей установки без адресов и секретов:

```bash
systemctl start lab-node-update.service && systemctl show --no-pager lab-node-update.service -p Result -p ExecMainStatus && systemctl is-active lab-node-agent.service lab-node-segment-helper.service lab-node-network-guard.service lab-guacamole-egress.service
runuser -u lab-node-agent -- /opt/lab-manager-node/current/venv/bin/python -c 'from lab_node_agent.segment_client import SegmentClient; s=SegmentClient().readiness(); print(s["ready"]); assert s["ready"] is True'
```
