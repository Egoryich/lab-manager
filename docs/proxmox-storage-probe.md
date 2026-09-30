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

В присланном выводе команда `pvesm config student-lvm` завершилась ошибкой `unknown command`; она не меняла конфигурацию. Эта команда была ошибочно предложена в чате. По [документации Proxmox](https://pve.proxmox.com/pve-docs/pvesm.1.html) конфигурация storage хранится в `/etc/pve/storage.cfg`, а [`pvesh`](https://github.com/proxmox/pve-docs/blob/master/pvesh.adoc) даёт доступ к API. Исправленная read-only команда `pvesh get /storage --output-format json` подтвердила для `student-lvm`: `type=lvmthin`, `vgname=student-lvm`, `thinpool=student-lvm`, `content=images,rootdir`, `nodes=pve`, без явного `disable`. Это согласуется с измеренным VG и thin pool.

Подтверждённые команды без серийных номеров и секретов:

```bash
pvs --reportformat json --units b --nosuffix \
  -o pv_name,vg_name,pv_size,pv_free
vgs --reportformat json --units b --nosuffix \
  -o vg_name,vg_size,vg_free,pv_count
pvesh get /storage --output-format json |
python3 -c 'import json,sys; rows=[{k:s.get(k) for k in ("storage","type","vgname","thinpool","content","nodes","disable")} for s in json.load(sys.stdin) if s.get("storage")=="student-lvm"]; assert len(rows)==1; print(json.dumps(rows[0],ensure_ascii=False))'
```

Один PV на VG пока не доказывает пригодность storage для admission: остаются сверка устройства и thin pool, всех guest/snapshot-томов и постоянных обязательств Lab Manager. `physical_backing_reconciled` и `commitments_reconciled` остаются ложными.

## Постоянная PV/VG-сверка (wheel 0.5.0)

Следующий выпуск root-задачи получает отдельные read-only отчёты `lvs`, `pvs` и `vgs` с фиксированными аргументами. Для каждого настроенного thin pool он сопоставляет VG, число PV, размеры и невыделенное место; несоответствие или ошибка любого отчёта не публикует новый снимок. В JSON появляется диагностический `backing` с именами устройств, размерами PV/VG и неизменно ложным `physical_backing_reconciled`. Невыделенное место VG не складывается с доступным местом thin pool. mTLS-агент продолжает только читать файл, `admission_ready=false` сохраняется.

Это ещё не подтверждение отдельного физического диска и не разрешение броней: следующим шагом нужны связь PV с блочным устройством, проверка состояния диска и учёт всех постоянных томов.

### Проверенная подготовка 0.5.0

CI выпуска `40aa370d6a094bb23c049ac29ebb3499389e8e7e` прошёл. Пользователь проверил внешний SHA256 `SHA256SUMS` и все шесть файлов выпуска, установил wheel 0.5.0 в отдельный venv и выполнил `systemd-analyze verify` без сообщений об ошибке. Ручной `host_storage` сопоставил `local-lvm` с PV `/dev/sdb3` и невыделенными 9 663 676 416 байт VG; `student-lvm` — с PV `/dev/sda` и 125 829 120 байт VG. Оба пула вернули `physical_backing_reconciled=false`, весь снимок — `admission_ready=false`. Работающие службы на этом шаге не переключались.

Обобщённый блок успешной подготовки (SHA выпуска и хеш manifest брать из успешного CI):

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
for asset in lab_node_agent-0.5.0-py3-none-any.whl \
             lab-node-agent.service lab-node-storage-snapshot.service \
             lab-node-storage-snapshot.timer node-pki.py SHA256SUMS; do
  curl --fail --location --retry 3 --connect-timeout 15 --max-time 180 \
    --proto '=https' --proto-redir '=https' -o "$asset" \
    "$release_url/$asset"
done
printf '%s  %s\n' "$manifest_sha256" SHA256SUMS | sha256sum --check --strict
sha256sum --check --strict SHA256SUMS
python3 -m venv "$stage/venv" </dev/null
"$stage/venv/bin/python" -m pip --disable-pip-version-check install \
  --no-index --no-deps "$stage/lab_node_agent-0.5.0-py3-none-any.whl" </dev/null
systemd-analyze verify "$stage/lab-node-storage-snapshot.service"
"$stage/venv/bin/python" -m lab_node_agent.host_storage
```

### Подтверждённое переключение на 0.5.0

После подготовки пользователь повторно проверил все файлы по `SHA256SUMS`, неизменённость agent/timer units и текущего storage unit. Он остановил таймер, заменил только storage service (увеличен timeout), атомарно переключил `current` и запустил oneshot. `read_snapshot()` от пользователя `lab-node-agent` прочитал два thin pool с полем `backing`; для обоих `physical_backing_reconciled=false`, общий `admission_ready=false`. Агент и таймер снова запустились, активный symlink указывает на 0.5.0, oneshot вернул `Result=success`, `ExecMainStatus=0`. Скрипт переключения включал trap для возврата старого symlink, unit и служб при ошибке. Немедленный вывод `list-timers` содержал `NEXT -`, поэтому периодичность и доставку на VPS нужно проверить отдельно.

Обобщённые команды успешного пути после проверенной подготовки; перед новым выпуском снова сверить изменённые systemd units и адаптировать проверку снимка к его схеме:

```bash
set -euo pipefail
test "$(id -u)" -eq 0
release='<verified-full-sha>'
base=/opt/lab-manager-node
stage="$base/releases/$release"
old=$(readlink -f "$base/current")
unit=/etc/systemd/system/lab-node-storage-snapshot.service
test -x "$stage/venv/bin/python"
test -x "$old/venv/bin/python"
test "$old" != "$stage"
systemctl is-active --quiet lab-node-agent.service
systemctl is-active --quiet lab-node-storage-snapshot.timer
cmp "$old/lab-node-storage-snapshot.service" "$unit"
cmp "$stage/lab-node-agent.service" /etc/systemd/system/lab-node-agent.service
cmp "$stage/lab-node-storage-snapshot.timer" \
  /etc/systemd/system/lab-node-storage-snapshot.timer
(cd "$stage" && sha256sum --check --strict SHA256SUMS)
chmod 0755 "$stage"
chmod -R a+rX "$stage/venv"

rollback() {
  rc=$1
  [ "$rc" -ne 0 ] || return 0
  trap - EXIT
  set +e
  systemctl disable --now lab-node-storage-snapshot.timer
  systemctl stop lab-node-storage-snapshot.service
  ln -s "$old" "$base/current.rollback" &&
    mv -Tf "$base/current.rollback" "$base/current"
  install -o root -g root -m 0644 "$old/lab-node-storage-snapshot.service" "$unit"
  systemctl daemon-reload
  systemctl start lab-node-storage-snapshot.service
  systemctl restart lab-node-agent.service
  systemctl enable --now lab-node-storage-snapshot.timer
  exit "$rc"
}
test ! -e "$base/current.next" && test ! -L "$base/current.next"
test ! -e "$base/current.rollback" && test ! -L "$base/current.rollback"
trap 'rollback $?' EXIT

systemctl disable --now lab-node-storage-snapshot.timer
systemctl stop lab-node-storage-snapshot.service
install -o root -g root -m 0644 "$stage/lab-node-storage-snapshot.service" "$unit"
systemctl daemon-reload
ln -s "$stage" "$base/current.next"
mv -Tf "$base/current.next" "$base/current"
systemctl start lab-node-storage-snapshot.service
test "$(systemctl show -P Result lab-node-storage-snapshot.service)" = success
runuser -u lab-node-agent -- "$stage/venv/bin/python" -c '
from lab_node_agent.host_storage import read_snapshot
s = read_snapshot()
pools = s["thin_pools"]
assert len(pools) == 2
assert all(p["backing"]["physical_backing_reconciled"] is False for p in pools)
assert s["admission_ready"] is False
print("Пулов:", len(pools), "допуск:", s["admission_ready"])
'
systemctl restart lab-node-agent.service
systemctl is-active --quiet lab-node-agent.service
systemctl enable --now lab-node-storage-snapshot.timer
systemctl is-active --quiet lab-node-storage-snapshot.timer
trap - EXIT
systemctl show --no-pager lab-node-storage-snapshot.service -p Result -p ExecMainStatus
```

### Повторный запуск и доставка на VPS подтверждены

После переключения список таймеров показал следующий запуск через 15 секунд и последний успешный запуск 34 секунды назад. Журнал содержал три последовательных успешных oneshot-запуска примерно через 50 секунд; `Result=success`, `ExecMainStatus=0`. VPS worker по mTLS получил `backing` для обоих пулов: `local-lvm` на `/dev/sdb3`, `student-lvm` на `/dev/sda`. Для обоих `physical_backing_reconciled=false`, в целом `admission_ready=false`. Это подтверждает регулярную доставку read-only диагностики; безопасный допуск к созданию машин ещё не реализован.

Проверенные команды без адресов и секретов:

```bash
# Proxmox
systemctl list-timers --all lab-node-storage-snapshot.timer --no-pager
systemctl show --no-pager lab-node-storage-snapshot.service -p Result -p ExecMainStatus
journalctl -u lab-node-storage-snapshot.service -n 9 --no-pager

# VPS, из каталога приложения
docker compose --env-file .env.vps -f infra/vps/compose.yml exec -T worker \
  /app/.venv/bin/python -c '
import json
from lab_manager.node_transport import load_endpoints, fetch
d, _ = fetch(load_endpoints("/run/lab-node-transport/nodes.json")[0])
s = d["sample"]
print(json.dumps({
    "pools": [
        {
            "storage": p["storage"],
            "devices": [v["name"] for v in p["backing"]["physical_volumes"]],
            "reconciled": p["backing"]["physical_backing_reconciled"],
        }
        for p in s["local_thin_pools"]
    ],
    "admission_ready": s["admission_ready"],
}, ensure_ascii=False))
'
```

### Проверка цепочки PV → блочное устройство

Пользователь выполнил `lsblk` и `findmnt` с фиксированными JSON-полями. В полученном снимке `/dev/sda` — целый диск размером 500 107 862 016 байт, содержащий thin pool `student-lvm`; `/dev/sdb` — другой диск, а его `/dev/sdb3` содержит системный `pve-root` и thin pool `local-lvm`. `findmnt` отдельно подтвердил источник `/` как `/dev/mapper/pve-root` (`ext4`). Это согласуется с ранее прочитанными LVM PV/VG, но имена `/dev/sd*` могут меняться при перезагрузке; один ручной снимок не подтверждает здоровье диска и постоянную доступность места. `physical_backing_reconciled=false` и `admission_ready=false` сохраняются.

Подтверждённые команды без серийных номеров и секретов:

```bash
lsblk --json --bytes --tree \
  --output NAME,PATH,TYPE,SIZE,PKNAME,MOUNTPOINTS
findmnt --json --mountpoint / \
  --output SOURCE,TARGET,FSTYPE
```

## Автоматическая диагностика блочной топологии (wheel 0.6.0)

Подготовлен следующий read-only выпуск root-задачи: она запускает фиксированные `lsblk` и `findmnt` после LVM-отчётов и связывает каждый PV с текущими дисками-предками. Повторяющиеся device-mapper узлы допускаются только с одинаковыми типом, размером и точками монтирования. Для PV в диагностическом `topology` публикуются тип блочного устройства, размер, список дисков-предков, признак целого диска и пересечение с дисками системного `/`. Отсутствующий PV, нераспознанный источник `/`, противоречащий отчёт или меньший размер блочного устройства отклоняют новый снимок; старый снимок перестаёт считаться свежим через 120 секунд.

Чистый парсер проверен на присланных JSON: `student-lvm` определён как отдельный целый диск, `local-lvm` — как раздел системного диска. Локальные тесты включают пропавший PV, иной корневой источник, неверный размер и противоречащие повторы device-mapper. Важная граница: это текущая топология, а не гарантия стабильности имён `/dev/sd*`, здоровья носителя или полноты дисковых обязательств. `physical_backing_reconciled=false` и `admission_ready=false` остаются неизменными. Переключение работающих служб на 0.6.0 записывается отдельно после проверки пользователем.

### Проверенная подготовка 0.6.0

[CI выпуска `e3a2b70733ec991c468e36771d7ad2daab27248c`](https://github.com/Egoryich/lab-manager/actions/runs/36717299451) завершился успешно и опубликовал wheel 0.6.0. Пользователь скачал шесть файлов в отдельный каталог, сверил внешний SHA-256 файла `SHA256SUMS` и все внутренние хеши, установил wheel без внешних зависимостей. Все три systemd unit совпали с установленными; `systemd-analyze verify` не сообщил ошибок. Ручной сбор дал `local-lvm` на разделе `/dev/sdb3` системного `/dev/sdb` и `student-lvm` на целом отдельном `/dev/sda`; `physical_backing_reconciled=false`, `admission_ready=false`. Службы и symlink `current` на этом шаге не переключались.

Обобщённые подтверждённые команды подготовки (SHA выпуска и хеш manifest брать из успешного CI, адреса и секреты не сохранять):

```bash
set -euo pipefail
umask 077
test "$(id -u)" -eq 0
release='<verified-full-sha>'
release_url='<trusted-release-url>'
manifest_sha256='<verified-manifest-sha256>'
stage="/opt/lab-manager-node/releases/$release"
test ! -e "$stage"
install -d -m 0700 "$stage"
cd "$stage"
for asset in lab_node_agent-0.6.0-py3-none-any.whl \
             lab-node-agent.service lab-node-storage-snapshot.service \
             lab-node-storage-snapshot.timer node-pki.py SHA256SUMS; do
  curl --fail --location --retry 3 --connect-timeout 15 --max-time 180 \
    --proto '=https' --proto-redir '=https' -o "$asset" "$release_url/$asset"
done
printf '%s  %s\n' "$manifest_sha256" SHA256SUMS | sha256sum --check --strict
sha256sum --check --strict SHA256SUMS
python3 -m venv "$stage/venv" </dev/null
"$stage/venv/bin/python" -m pip --disable-pip-version-check install \
  --no-index --no-deps "$stage/lab_node_agent-0.6.0-py3-none-any.whl" </dev/null
cmp "$stage/lab-node-agent.service" /etc/systemd/system/lab-node-agent.service
cmp "$stage/lab-node-storage-snapshot.service" \
  /etc/systemd/system/lab-node-storage-snapshot.service
cmp "$stage/lab-node-storage-snapshot.timer" \
  /etc/systemd/system/lab-node-storage-snapshot.timer
systemd-analyze verify "$stage/lab-node-storage-snapshot.service"
"$stage/venv/bin/python" -m lab_node_agent.host_storage
```

### Подтверждённое переключение на 0.6.0

Пользователь проверил версии wheel 0.5.0/0.6.0, все файлы по `SHA256SUMS` и неизменность трёх systemd unit. После атомарного переключения `current` oneshot записал снимок с новой топологией. `read_snapshot()` от `lab-node-agent` подтвердил `student-lvm` на отдельном целом диске и `local-lvm` на системном разделе; оба признака `physical_backing_reconciled` и общий `admission_ready` остались ложными. Агент и таймер снова стали активными, oneshot вернул `Result=success`, `ExecMainStatus=0`; активный symlink указывает на выпуск 0.6.0. Как и при 0.5.0, мгновенный вывод таймера содержал `NEXT -`: периодичность и доставку на VPS проверить отдельным шагом.

Обобщённый успешный порядок с возвратом предыдущего wheel при ошибке. Для другого сервера/выпуска скорректировать ожидаемые версии, имена storage и проверки топологии до запуска:

```bash
set -euo pipefail
test "$(id -u)" -eq 0
release='<verified-full-sha>'
base=/opt/lab-manager-node
stage="$base/releases/$release"
old=$(readlink -f "$base/current")
test -x "$stage/venv/bin/python"
test -x "$old/venv/bin/python"
test "$old" != "$stage"
test ! -e "$base/current.next" && test ! -L "$base/current.next"
test ! -e "$base/current.rollback" && test ! -L "$base/current.rollback"
systemctl is-active --quiet lab-node-agent.service
systemctl is-active --quiet lab-node-storage-snapshot.timer
cmp "$stage/lab-node-agent.service" /etc/systemd/system/lab-node-agent.service
cmp "$stage/lab-node-storage-snapshot.service" \
  /etc/systemd/system/lab-node-storage-snapshot.service
cmp "$stage/lab-node-storage-snapshot.timer" \
  /etc/systemd/system/lab-node-storage-snapshot.timer
(cd "$stage" && sha256sum --check --strict SHA256SUMS)
chmod 0755 "$stage"
chmod -R a+rX "$stage/venv"

rollback() {
  rc=$1
  trap - EXIT
  set +e
  systemctl disable --now lab-node-storage-snapshot.timer
  systemctl stop lab-node-storage-snapshot.service
  rm -f "$base/current.next"
  ln -s "$old" "$base/current.rollback" &&
    mv -Tf "$base/current.rollback" "$base/current"
  systemctl start lab-node-storage-snapshot.service
  systemctl restart lab-node-agent.service
  systemctl enable --now lab-node-storage-snapshot.timer
  exit "$rc"
}
trap 'rollback $?' EXIT
systemctl disable --now lab-node-storage-snapshot.timer
systemctl stop lab-node-storage-snapshot.service
ln -s "$stage" "$base/current.next"
mv -Tf "$base/current.next" "$base/current"
systemctl start lab-node-storage-snapshot.service
test "$(systemctl show -P Result lab-node-storage-snapshot.service)" = success
runuser -u lab-node-agent -- "$stage/venv/bin/python" -c '
from lab_node_agent.host_storage import read_snapshot
s = read_snapshot()
pools = {p["storage"]: p for p in s["thin_pools"]}
assert set(pools) == {"local-lvm", "student-lvm"}
student = pools["student-lvm"]["backing"]["physical_volumes"]
system = pools["local-lvm"]["backing"]["physical_volumes"]
assert len(student) == len(system) == 1
assert student[0]["topology"]["whole_disk"] is True
assert student[0]["topology"]["shares_system_disk"] is False
assert system[0]["topology"]["shares_system_disk"] is True
assert all(p["backing"]["physical_backing_reconciled"] is False for p in pools.values())
assert s["admission_ready"] is False
'
systemctl restart lab-node-agent.service
systemctl is-active --quiet lab-node-agent.service
systemctl enable --now lab-node-storage-snapshot.timer
systemctl is-active --quiet lab-node-storage-snapshot.timer
trap - EXIT
systemctl show --no-pager lab-node-storage-snapshot.service -p Result -p ExecMainStatus
```

### Периодический сбор 0.6.0 и доставка на VPS подтверждены

После переключения пользователь показал три последовательных успешных запуска oneshot примерно через 50 секунд, запланированный следующий запуск таймера, `Result=success` и `ExecMainStatus=0`. VPS worker по mTLS получил `topology` обоих PV: `local-lvm` — раздел `/dev/sdb3` на системном `/dev/sdb`; `student-lvm` — отдельный целый `/dev/sda`. В переданном снимке `admission_ready=false`. Это подтверждает работающую цепочку локальный read-only сбор → агент → VPS; не подтверждает устойчивость имён `/dev/sd*`, состояние диска или доступный лимит для занятия.

Проверенные команды без адресов и секретов:

```bash
# Proxmox
systemctl list-timers --all lab-node-storage-snapshot.timer --no-pager
systemctl show --no-pager lab-node-storage-snapshot.service -p Result -p ExecMainStatus
journalctl -u lab-node-storage-snapshot.service -n 9 --no-pager

# VPS, из каталога приложения
docker compose --env-file .env.vps -f infra/vps/compose.yml exec -T worker \
  /app/.venv/bin/python -c '
import json
from lab_manager.node_transport import load_endpoints, fetch
d, _ = fetch(load_endpoints("/run/lab-node-transport/nodes.json")[0])
s = d["sample"]
print(json.dumps({
    "pools": [
        {
            "storage": p["storage"],
            "pv": [{"name": v["name"], "topology": v["topology"]}
                   for v in p["backing"]["physical_volumes"]],
        }
        for p in s["local_thin_pools"]
    ],
    "admission_ready": s["admission_ready"],
}, ensure_ascii=False))
'
```

## Устойчивые идентификаторы LVM (wheel 0.7.0)

Пользователь подтвердил read-only `pvs` и `vgs` в JSON: два PV на двух VG, у каждого есть отдельный LVM UUID. Текущие пути PV совпали с наблюдаемыми `/dev/sda` и `/dev/sdb3`, а размеры и свободное невыделенное место VG остались прежними. UUID не публикуются в этом руководстве: важно само наличие и уникальность идентификаторов, а не значения конкретной установки.

Проверенные команды без адресов и секретов:

```bash
pvs --reportformat json --units b --nosuffix \
  -o pv_name,pv_uuid,vg_name,pv_size,pv_free
vgs --reportformat json --units b --nosuffix \
  -o vg_name,vg_uuid,vg_size,vg_free,pv_count
```

В подготовленном wheel 0.7.0 root-задача добавляет UUID в постоянный снимок и отклоняет отсутствующие, неверные и повторные UUID. Это диагностический признак изменения LVM-томов. Для обычных операций Lab Manager использует имя хранилища Proxmox и Proxmox API; отдельная ручная привязка учебного storage к UUID не требуется. Пока UUID используются только для диагностики: `physical_backing_reconciled=false` и `admission_ready=false`; CPU/RAM/диск не резервируются по этому наблюдению.

### Проверенная подготовка wheel 0.7.0

Linux CI завершился успешно. На Proxmox загружены wheel, systemd units, PKI-скрипт и `SHA256SUMS`; отдельно проверен опубликованный SHA-256 самого манифеста, затем все перечисленные в нём файлы. Из wheel создано отдельное виртуальное окружение, установлен `lab-node-agent==0.7.0`. Прямой read-only сбор с хоста подтвердил UUID каждого PV/VG и сохранил `admission_ready=false`. Работающий агент при этой проверке не переключался.

Повторяемые команды проверки после загрузки артефактов (значение ожидаемого SHA-256 манифеста берётся из доверенного выпуска):

```bash
printf '%s  %s\n' "$MANIFEST_SHA256" SHA256SUMS | sha256sum --check --strict
sha256sum --check --strict SHA256SUMS
python3 -m venv "$stage/venv"
"$stage/venv/bin/python" -m pip --disable-pip-version-check install \
  --no-index --no-deps "$stage/lab_node_agent-0.7.0-py3-none-any.whl"
"$stage/venv/bin/python" -m lab_node_agent.host_storage |
python3 -c '
import json, sys
s = json.load(sys.stdin)
assert all(p["backing"]["vg_uuid"] and
           all(pv["pv_uuid"] for pv in p["backing"]["physical_volumes"])
           for p in s["thin_pools"])
assert s["admission_ready"] is False
print("LVM UUIDs present; admission remains disabled")
'
```

### Переключение на 0.7.0 подтверждено

На Proxmox проверены версии старого и нового wheel, совпадение всех установленных systemd units с выпуском и контрольные суммы файлов. Затем таймер был остановлен, символьная ссылка `current` переключена на новый выпуск, root-задача создала снимок, непривилегированный агент прочитал его, сервис и таймер были запущены. Пользователь подтвердил путь активного выпуска 0.7.0, `Result=success`, `ExecMainStatus=0` и `admission_ready=false`. Скрипт имел обработчик отката к предыдущему выпуску при ошибке.

Повторяемая проверка после переключения (не раскрывает UUID):

```bash
readlink -f /opt/lab-manager-node/current
systemctl is-active lab-node-agent.service lab-node-storage-snapshot.timer
systemctl show --no-pager lab-node-storage-snapshot.service \
  -p Result -p ExecMainStatus
runuser -u lab-node-agent -- \
  /opt/lab-manager-node/current/venv/bin/python -c '
from lab_node_agent.host_storage import read_snapshot
s = read_snapshot()
assert all(p["backing"]["vg_uuid"] and
           all(pv["pv_uuid"] for pv in p["backing"]["physical_volumes"])
           for p in s["thin_pools"])
assert s["admission_ready"] is False
print("LVM UUIDs present; admission remains disabled")
'
```

Перед включением создания машин нужны выбор допустимого Proxmox storage по его имени, сверка дисковых обязательств и ресурсов, проверка сети и шлюза. UUID можно использовать для предупреждения администратора о неожиданной замене или пересоздании LVM-тома, но не как обязательную ручную настройку для преподавателя. Наличие UUID само по себе не снимает ограничений.

### Периодический сбор и доставка UUID на VPS подтверждены

После переключения на 0.7.0 таймер показал следующий запуск и три последовательных успешных oneshot-запуска примерно через 50 секунд. `Result=success`, `ExecMainStatus=0`. VPS worker по mTLS получил новый снимок обоих thin pool; у каждого есть VG UUID и PV UUID. В переданном снимке `admission_ready=false`. Значения UUID в журнал не записываются.

Проверенные команды без адресов и секретов:

```bash
# Proxmox
systemctl list-timers --all lab-node-storage-snapshot.timer --no-pager
systemctl show --no-pager lab-node-storage-snapshot.service \
  -p Result -p ExecMainStatus
journalctl -u lab-node-storage-snapshot.service -n 9 --no-pager

# VPS, из каталога приложения
docker compose --env-file .env.vps -f infra/vps/compose.yml exec -T worker \
  /app/.venv/bin/python -c '
from lab_manager.node_transport import load_endpoints, fetch
d, _ = fetch(load_endpoints("/run/lab-node-transport/nodes.json")[0])
s = d["sample"]
pools = {p["storage"]: p for p in s["local_thin_pools"]}
assert {"local-lvm", "student-lvm"} <= pools.keys()
for name in ("local-lvm", "student-lvm"):
    backing = pools[name]["backing"]
    assert backing["vg_uuid"]
    assert all(pv["pv_uuid"] for pv in backing["physical_volumes"])
assert s["admission_ready"] is False
print("Снимок:", s["sample_finished_at"])
print("Пулы с UUID:", ", ".join(sorted(pools)))
print("Допуск машин:", s["admission_ready"])
'
```
