# Защита будущих учебных мостов

Статус на 01.10.2026: пользователь установил deny-only правила на Proxmox из выпуска `dcb291738979b158c1894a990af74fe8acfe2e99`. Три скачанных файла прошли SHA-256 проверку, установщик завершился сообщением `Deny-only guard installed; no guest bridge was attached`, а systemd показал `ActiveState=active`, `Result=success`, `ExecMainStatus=0`. Это подтверждает первое применение; восстановление после reboot и сохранность при reload других firewall-служб ещё не проверены. Рабочие `vmbr0`, `vmbr1` и Tailscale не менялись установщиком.

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

Эти команды предполагают локальный checkout. На реальном узле успешно применён следующий способ без checkout: скачать три файла с закреплённого коммита, проверить SHA-256 и вызвать тот же установщик. Исторические SHA-256 этого выпуска: `lab-network-guard.nft` — `1e699346bbe28f0a0b1d97828a83369ee449770714e5523085026fc374ce9118`, `lab-node-network-guard.service` — `68282a2dfb265a3001179dc9bfd8d817dbf39243672cc5b8815e4d942b29c69f`, `install-node-network-guard.sh` — `ab9944804a64204592f4bd98aeae04a7d4c5805c0847d7ae88656bf639abdc61`.

```bash
rev='dcb291738979b158c1894a990af74fe8acfe2e99'
dir=$(mktemp -d)
trap 'rm -f "$dir/lab-network-guard.nft" "$dir/lab-node-network-guard.service" "$dir/install-node-network-guard.sh"; rmdir "$dir"' EXIT
for path in infra/proxmox/lab-network-guard.nft \
            infra/proxmox/lab-node-network-guard.service \
            tools/install-node-network-guard.sh; do
  curl --fail --silent --show-error --location --retry 3 \
    --proto '=https' --proto-redir '=https' \
    -o "$dir/${path##*/}" \
    "https://raw.githubusercontent.com/Egoryich/lab-manager/$rev/$path"
done
printf '%s  %s\n' \
  '1e699346bbe28f0a0b1d97828a83369ee449770714e5523085026fc374ce9118' "$dir/lab-network-guard.nft" \
  '68282a2dfb265a3001179dc9bfd8d817dbf39243672cc5b8815e4d942b29c69f' "$dir/lab-node-network-guard.service" \
  'ab9944804a64204592f4bd98aeae04a7d4c5805c0847d7ae88656bf639abdc61' "$dir/install-node-network-guard.sh" |
  sha256sum --check --strict
bash "$dir/install-node-network-guard.sh" \
  "$dir/lab-network-guard.nft" \
  "$dir/lab-node-network-guard.service"
systemctl show --no-pager lab-node-network-guard.service \
  -p ActiveState -p Result -p ExecMainStatus
```

Команда выполняется под root. Она предназначена для первой установки; повторное выполнение штатно остановится на проверке существующей службы. Не включать `nftables.service` с его `/etc/nftables.conf` без отдельного разбора: чужой полный reload может удалить таблицы Lab Manager.

Остаётся проверить сохранение после reboot и после reload firewall, состояние таблиц при drift, правила против spoofing, реальные LXC/VM, Guacamole и Internet on/off. До этих проверок `NETWORK_AND_GATEWAY_NOT_VERIFIED` и `admission_ready=false` сохраняются.
