# Локальная диагностика LVM-thin на Proxmox

`lab_node_agent.host_storage` — диагностический модуль wheel 0.3.0. Администратор запускает его **локально от root**; установленная непривилегированная служба агента не вызывает `lvs` и не получает дополнительные права. Модуль читает только секции `lvmthin` из `/etc/pve/storage.cfg` и фиксированной командой `lvs` получает размеры пулов, заполнение data/metadata и список томов. Он выводит JSON без токена, содержимого конфигураций машин и других секций storage.cfg. Ошибки выводятся фиксированным кодом.

API содержимого Proxmox недостаточно для этого этапа: его read-only endpoint доступен с `Datastore.Audit`, но по [исходному коду Proxmox](https://github.com/proxmox/pve-storage/blob/master/src/PVE/API2/Storage/Content.pm) каждый том дополнительно проходит проверку доступа и может быть пропущен. Текущий read-only token не получает права изменения дисков ради полноты списка. Формат `lvs --reportformat json` и поля LVM описаны в [руководстве lvs](https://man7.org/linux/man-pages/man8/lvs.8.html).

На подготовленном Proxmox после проверки SHA выпуска и wheel:

```bash
# stage указывает на каталог с проверенным wheel новой версии.
python3 -m venv "$stage/venv" </dev/null
"$stage/venv/bin/python" -m pip --disable-pip-version-check install \
  --no-index --no-deps "$stage/lab_node_agent-0.3.0-py3-none-any.whl" </dev/null
"$stage/venv/bin/python" -m lab_node_agent.host_storage
```

Поле `owner_vmid_from_name` — лишь число из имени LV, а не подтверждение владельца. `ownership_reconciled=false` и `admission_ready=false` обязательны даже при успешном выводе. Данные ещё не связываются с бронями и гостевыми конфигурациями; для полного допуска также нужны проверка снимков, физической основы storage, управление созданными Lab Manager томами и синхронизация наблюдения с операциями. Чужие и тестовые диски нельзя считать свободными и нельзя удалять автоматически.

После реальной проверки команды и безопасные результаты дополняются здесь без адресов, секретов и серийных номеров устройств.

## Первая проверка на физическом сервере

Пользователь запустил wheel выпуска `bbd5e3c7120ac0fe68db7495432706b957560297` отдельно от службы. Он подтвердил внешний SHA256 файла `SHA256SUMS`, затем проверку всех четырёх release assets, установку wheel 0.3.0 в отдельный venv и `active` у прежней службы. Сводка локального read-only probe показала:

Обобщённая последовательность успешной подготовки (адрес релиза, SHA и ожидаемый хеш manifest брать из проверенного CI выпуска; секреты и адреса серверов не нужны):

```bash
set -euo pipefail
umask 077
release='<verified-full-sha>'
release_url='<trusted-release-url>'
manifest_sha256='<verified-manifest-sha256>'
stage="/opt/lab-manager-node/releases/$release"
test ! -e "$stage"
install -d -m 0700 "$stage"
cd "$stage"
for asset in lab_node_agent-0.3.0-py3-none-any.whl \
             lab-node-agent.service node-pki.py SHA256SUMS; do
  curl --fail --location --retry 3 --connect-timeout 15 --max-time 180 \
    --proto '=https' --proto-redir '=https' -o "$asset" \
    "$release_url/$asset"
done
printf '%s  %s\n' "$manifest_sha256" SHA256SUMS | sha256sum --check --strict
sha256sum --check --strict SHA256SUMS
python3 -m venv "$stage/venv" </dev/null
"$stage/venv/bin/python" -m pip --disable-pip-version-check install \
  --no-index --no-deps "$stage/lab_node_agent-0.3.0-py3-none-any.whl" </dev/null
"$stage/venv/bin/python" -m lab_node_agent.host_storage
systemctl is-active lab-node-agent.service
```

| Storage | VG / thin pool | Pool bytes | Data | Metadata | Найдено томов |
| --- | --- | ---: | ---: | ---: | ---: |
| `local-lvm` | `pve` / `data` | 67 641 540 608 | 0.0% | 1.6% | 0 |
| `student-lvm` | `student-lvm` / `student-lvm` | 489 970 204 672 | 0.0% | 0.38% | 0 |

В прежнем диагностическом выводе `lvs` были тестовые `vm-100-disk-0` и `vm-201-disk-0`; данный снимок их не показывает. Независимая сверка полного списка LVM и конфигураций гостей приведена ниже. Нулевой счётчик томов сам по себе **не означает**, что дисковые обязательства равны нулю: нужны также брони и снапшоты. `admission_ready=false` сохраняется.

### Независимая сверка

Следующая проверка пользователя сравнила полный JSON-отчёт `lvs --all` с обоими списками гостей Proxmox (`qemu` и `lxc`). В отчёте остались только два thin pool: `pve/data` размером 67 641 540 608 байт и `student-lvm/student-lvm` размером 489 970 204 672 байта. Строк с атрибутом виртуального thin LV (`V…`) не было; оба списка гостей пусты. Это подтверждает нулевой список машин и thin-томов **на момент этой проверки**. Для будущего выделения места всё равно нужна сверка с бронями, снапшотами и физическим запасом.

Повторяемые команды без адресов и секретов:

```bash
lvs --all --reportformat json --units b --nosuffix \
  -o vg_name,lv_name,lv_attr,pool_lv,lv_size,data_percent,metadata_percent
for kind in qemu lxc; do
  pvesh get "/nodes/$(hostname)/$kind" --output-format json
done
```

## Регулярный локальный снимок (wheel 0.4.0)

Новая версия добавляет отдельные `lab-node-storage-snapshot.service` и `.timer`. Задача раз в ~45 секунд запускает только фиксированную read-only команду LVM от root и атомарно записывает `/var/lib/lab-manager-node/storage.json` (root:lab-node-agent, 0640). Сетевая служба остаётся непривилегированной и **не выполняет `lvs`**. Она принимает лишь свежий файл с проверенным владельцем, группой, правами и форматом. Если файл отсутствует, устарел, не совпадает с данными Proxmox по именам/размерам пулов или повреждён, thin metadata не публикуется как подтверждённая и в ограничениях появляется соответствующий код. Чужие тома остаются `UNVERIFIED`, а `admission_ready=false` при любом результате.

Служба и таймер поставляются вместе с wheel и хешируются в `SHA256SUMS`. Сначала установить проверенный выпуск и единоразово запустить oneshot, проверить свежий файл и только затем включить timer и обновить сетевой агент. Подтверждённый порядок переключения приведён ниже.

### Проверенная установка 0.4.0

Пользователь проверил выпуск `dcdbd815f54d7884c20b5e80b8a8bd4a342a59a5`: SHA-256 manifest и всех шести файлов совпали; wheel 0.4.0 установился в отдельный venv, `systemd-analyze verify` прошёл без замечаний, ручной probe нашёл два thin pool и ноль томов при `admission_ready=false`. Затем на Proxmox сравнили неизменённый `lab-node-agent.service`, сделали venv доступным системному пользователю, установили две новые unit-конфигурации, атомарно переключили `current` и запустили oneshot. Файл снимка получил UID 0, GID группы агента и mode 0640. Запуск `read_snapshot()` от `lab-node-agent` прочитал два пула, ноль томов и `admission_ready=false`. Таймер включился, агент перезапустился, oneshot вернул `Result=success` и `ExecMainStatus=0`. Доставка нового снимка на VPS и следующий запуск таймера проверяются отдельно.

Обобщённый порядок уже выполненных команд (SHA берётся из проверенного выпуска, без адресов и секретов):

```bash
set -euo pipefail
release='<verified-full-sha>'
stage="/opt/lab-manager-node/releases/$release"
old=$(readlink -f /opt/lab-manager-node/current)
test -x "$stage/venv/bin/python"
test -x "$old/venv/bin/python"
cmp "$stage/lab-node-agent.service" /etc/systemd/system/lab-node-agent.service
chmod 0755 "$stage"
chmod -R a+rX "$stage/venv"
install -o root -g root -m 0644 "$stage/lab-node-storage-snapshot.service" \
  /etc/systemd/system/lab-node-storage-snapshot.service
install -o root -g root -m 0644 "$stage/lab-node-storage-snapshot.timer" \
  /etc/systemd/system/lab-node-storage-snapshot.timer
systemctl daemon-reload
ln -s "$stage" /opt/lab-manager-node/current.next
mv -Tf /opt/lab-manager-node/current.next /opt/lab-manager-node/current
systemctl start lab-node-storage-snapshot.service
stat -c 'UID=%u GID=%g mode=%a' /var/lib/lab-manager-node/storage.json
runuser -u lab-node-agent -- "$stage/venv/bin/python" -c \
  'from lab_node_agent.host_storage import read_snapshot; s=read_snapshot(); print(len(s["thin_pools"]), s["admission_ready"])'
systemctl enable --now lab-node-storage-snapshot.timer
systemctl restart lab-node-agent.service
systemctl is-active --quiet lab-node-agent.service
systemctl is-active --quiet lab-node-storage-snapshot.timer
```

Во время подтверждённой установки использовался trap, возвращающий предыдущий symlink и агент при ошибке; приведённые команды описывают успешный путь и не заменяют процедуру отката. Сразу после `enable --now` таймер мог повторно запустить oneshot, поэтому поле `NEXT` в мгновенном выводе было пустым. Историческая ошибка агента после загрузки сервера завершилась его автоматическим перезапуском; её причина требует отдельной диагностики, если повторится.

### Периодический сбор и доставка подтверждены

Последующая проверка показала `active` у агента и таймера, `Result=success`, `ExecMainStatus=0` у oneshot. Журнал подтвердил четыре последовательных успешных запуска примерно через 50 секунд, а список таймеров показал следующий запуск. `read_snapshot()` от пользователя агента прочитал два пула, ноль томов и `admission_ready=False`.

На VPS существующий worker через `load_endpoints()` и mTLS `fetch()` получил оба локальных thin pool (по нулю томов), metadata 1.6% для `local-lvm` и 0.38% для `student-lvm`. Обычное файловое `local` по-прежнему имеет `thin_metadata_percent=null`. В ответе остались ограничения `NON_ATOMIC_OBSERVATION`, `ACL_FILTERING_POSSIBLE`, `DISK_COMMITMENTS_NOT_RECONCILED`, `NETWORK_AND_GATEWAY_NOT_VERIFIED`; `admission_ready=false`. Проверка доказывает доставку снимка, но не право на выделение ресурсов.

Подтверждённые проверки без адресов и секретов:

```bash
# Proxmox
systemctl is-active lab-node-agent.service lab-node-storage-snapshot.timer
systemctl show --no-pager lab-node-storage-snapshot.service -p Result -p ExecMainStatus
systemctl list-timers --all lab-node-storage-snapshot.timer --no-pager
journalctl -u lab-node-storage-snapshot.service -n 12 --no-pager
runuser -u lab-node-agent -- /opt/lab-manager-node/current/venv/bin/python -c \
  'from lab_node_agent.host_storage import read_snapshot; s=read_snapshot(); print(len(s["thin_pools"]), sum(len(p["volumes"]) for p in s["thin_pools"]), s["admission_ready"])'

# VPS, внутри /opt/lab-manager/repo
docker compose --env-file .env.vps -f infra/vps/compose.yml exec -T worker \
  /app/.venv/bin/python -c \
  'from lab_manager.node_transport import load_endpoints,fetch; d,_=fetch(load_endpoints("/run/lab-node-transport/nodes.json")[0]); s=d["sample"]; print([(p["storage"],len(p["volumes"])) for p in s.get("local_thin_pools",[])],s["admission_ready"])'
```

### Первичная проверка физических PV/VG

Пользователь выполнил read-only `pvs` и `vgs` в JSON. `student-lvm` имеет один PV `/dev/sda` размером 500 103 643 136 байт и 125 829 120 байт свободного **невыделенного места VG**. `pve` имеет один PV `/dev/sdb3` размером 118 107 406 336 байт и 9 663 676 416 байт свободного места VG. Ранее подтверждённый thin pool `student-lvm` имеет размер 489 970 204 672 байта. Свободные 125 МБ VG **не являются** свободным местом внутри thin pool и не должны использоваться для расчёта вместимости учебных машин.

В присланном выводе команда `pvesm config student-lvm` завершилась ошибкой `unknown command`; она не меняла конфигурацию. Эта команда была ошибочно предложена в чате. По [документации Proxmox](https://pve.proxmox.com/pve-docs/pvesm.1.html) конфигурация storage хранится в `/etc/pve/storage.cfg`, а [`pvesh`](https://github.com/proxmox/pve-docs/blob/master/pvesh.adoc) даёт доступ к API. Следующая read-only проверка должна использовать `pvesh get /storage --output-format json` и выводить только поля целевого storage; её результат на этом сервере ещё не подтверждён.

Подтверждённые команды без серийных номеров и секретов:

```bash
pvs --reportformat json --units b --nosuffix \
  -o pv_name,vg_name,pv_size,pv_free
vgs --reportformat json --units b --nosuffix \
  -o vg_name,vg_size,vg_free,pv_count
```

Один PV на VG пока не доказывает пригодность storage для admission: остаются сверка устройства и thin pool, всех guest/snapshot-томов и постоянных обязательств Lab Manager. `physical_backing_reconciled` и `commitments_reconciled` остаются ложными.
