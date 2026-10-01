# Защита будущих учебных мостов

Статус на 01.10.2026: исходники подготовлены, постоянные правила на Proxmox ещё не установлены. Рабочие `vmbr0`, `vmbr1` и Tailscale не меняются.

Подтверждены два независимых одноразовых теста на Proxmox: `inet input` блокировал пакет к host, `inet forward` — маршрутизируемый пакет между двумя временными мостами, `bridge forward` — кадры между двумя портами одного временного моста. Во всех тестах связь проходила до добавления правила и прекращалась после него. В L2-тесте пользователь дополнительно подтвердил удаление временного bridge. Это проверки механизма, а не готовая политика для учебных гостей.

Файл [lab-network-guard.nft](../infra/proxmox/lab-network-guard.nft) создаёт только две таблицы Lab Manager: `inet lab_manager` и `bridge lab_manager_l2`. Их правила запрещают трафик интерфейсов с зарезервированным префиксом `lmbr*` в host, routed и bridged путях. Другие интерфейсы не входят в условия. Такой набор блокирует новые учебные сети целиком, пока не применена полная политика с узкими разрешениями для Guacamole и выбранного доступа в Интернет. Это предохранитель, а не сеть, готовая к запуску машин.

Отдельная [systemd-служба](../infra/proxmox/lab-node-network-guard.service) заново загружает предохранитель при старте узла до `pve-guests.service`. Она не очищает глобальный ruleset и не выключает чужие firewall-службы. Lab Manager-owned машины должны иметь `onboot=0` и не запускаться, если проверка установленной policy не пройдена; одна только очерёдность systemd не является достаточным gate.

Для первого применения использовать только [установщик](../tools/install-node-network-guard.sh) с файлами из проверенного коммита. Он работает только под root, требует отсутствия интерфейсов `lmbr*`, обеих таблиц и прежней установки службы, отказывается при включённом или действующем `nftables.service`, сначала выполняет `nft --check` и `systemd-analyze verify`. Если проверка не прошла, файл правил не применяется. После установки он проверяет обе таблицы и активность службы.

```bash
bash tools/install-node-network-guard.sh \
  infra/proxmox/lab-network-guard.nft \
  infra/proxmox/lab-node-network-guard.service

systemctl show --no-pager lab-node-network-guard.service \
  -p ActiveState -p Result -p ExecMainStatus
nft list table inet lab_manager
nft list table bridge lab_manager_l2
```

Эти команды предполагают локальный checkout; для установленного узла передавать те же три файла с закреплённого коммита после SHA-256 проверки. Не включать штатный `nftables.service` с его `/etc/nftables.conf` без отдельного разбора: чужой полный reload может удалить таблицы Lab Manager.

Остаётся проверить сохранение после reboot и после reload firewall, состояние таблиц при drift, правила против spoofing, реальные LXC/VM, Guacamole и Internet on/off. До этих проверок `NETWORK_AND_GATEWAY_NOT_VERIFIED` и `admission_ready=false` сохраняются.
