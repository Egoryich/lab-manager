# Проверка транспорта гостевого выхода через VPS

Статус: подготовленная проверка, ещё не выполнена на хостах. DEC-18 требует выпускать гостевой Интернет через VPS и не менять default route самого Proxmox. Этот тест создаёт только временный GRE-интерфейс поверх уже работающего Headscale/Tailscale и маршрут к одному тестовому адресу внутри него. Он **не** добавляет гостевые подсети, NAT, forwarding или firewall-исключения и не подключает VM/LXC.

Tailscale документирует передачу IP-протоколов в Linux kernel mode ([источник](https://tailscale.com/kb/1177/kernel-vs-userspace-routers)); работа GRE с конкретным Headscale и tailnet policy требует измерения. Обычный выбор VPS как Tailscale exit node для Proxmox не используется: [exit node](https://tailscale.com/kb/1103/exit-nodes/) меняет маршрут всего нетailnet-трафика клиента. Management должен остаться на прежнем default route.

Перед проверкой на обоих хостах убедиться, что выбранная тестовая `/30` не пересекается с локальными маршрутами и интерфейс `lm-egress-test` отсутствует. Вместо значений в угловых скобках подставить IP уже существующих tailnet peer; не добавлять эти значения в репозиторий.

На VPS под root:

```bash
set -euo pipefail
vps_tail_ip='<VPS_TAILNET_IP>'
proxmox_tail_ip='<PROXMOX_TAILNET_IP>'
test ! -e /sys/class/net/lm-egress-test
test -z "$(ip -4 route show exact 169.254.61.0/30)"
ip tunnel add lm-egress-test mode gre \
  local "$vps_tail_ip" remote "$proxmox_tail_ip" ttl 64
ip addr add 169.254.61.1/30 dev lm-egress-test
ip link set dev lm-egress-test mtu 1200 up
ip -br addr show dev lm-egress-test
```

На Proxmox под root:

```bash
set -euo pipefail
vps_tail_ip='<VPS_TAILNET_IP>'
proxmox_tail_ip='<PROXMOX_TAILNET_IP>'
test ! -e /sys/class/net/lm-egress-test
test -z "$(ip -4 route show exact 169.254.61.0/30)"
ip tunnel add lm-egress-test mode gre \
  local "$proxmox_tail_ip" remote "$vps_tail_ip" ttl 64
ip addr add 169.254.61.2/30 dev lm-egress-test
ip link set dev lm-egress-test mtu 1200 up
ping -c 3 -W 2 -I lm-egress-test 169.254.61.1
ip -4 route show default
```

После пробы на **обоих** хостах: `ip tunnel del lm-egress-test`. При ошибке после создания интерфейса та же команда удаляет его; management route не менялся. Не переходить к постоянному routing/NAT при неуспехе теста. Для следующей фазы отдельно потребуются фильтры по учебным сегментам на Proxmox и VPS, policy routing только гостевого источника, запрет fallback через `vmbr0`, VPS SNAT и пакетные проверки с root-гостем.
