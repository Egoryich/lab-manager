# Подготовка отдельной Guacamole VM

Скрипт [prepare-guacamole-vm.sh](../tools/prepare-guacamole-vm.sh) создаёт выключенную Debian cloud VM на служебном storage Proxmox. Он не изменяет маршруты или firewall, не запускает VM и не устанавливает Guacamole. Это первый шаг до настройки выделенного сетевого выхода, Headscale и [Guacamole Compose](../infra/guacamole/compose.yml). Студенческие LXC остаются на `student-lvm`.

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

После первого успешного запуска надо проверить `qm config`, доступность VM из Proxmox, отсутствие прямого доступа студентов и доступ Guacamole только через VPS. Затем отдельно включить `onboot=1` и сохранить реально выполненные команды и результаты в этом гайде. До этих проверок это подготовка, а не подтверждённый production gateway.

Источники: [Debian cloud images](https://cloud.debian.org/images/cloud/trixie/latest/), [Proxmox Cloud-Init Support](https://pve.proxmox.com/wiki/Cloud-Init_Support).
