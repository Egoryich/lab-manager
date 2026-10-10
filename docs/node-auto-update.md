# Автообновление агента Proxmox

После однократной установки `lab-node-update.timer` примерно раз в пять минут проверяет `dev-proxmox` исходящим HTTPS-запросом. GitHub не подключается к узлу по SSH. VPS обновляется отдельным таймером из `dev-vps`; ветки и механизмы доставки независимы.

Updater принимает только новый потомок активного коммита, если последний push-run `check.yml` для точного SHA завершился успешно и jobs `identity-groups`, `node-agent`, `node-agent-release` имеют `success`. Релиз `node-agent-<SHA>` обязан указывать на этот коммит. Все скачанные файлы проверяются по SHA-256, опубликованным GitHub для assets, и дополнительно по `SHA256SUMS`.

Автоматически заменяется только wheel агента. Если `lab-node-agent.service` или storage snapshot service/timer изменились, updater останавливается до переключения: такой выпуск ставится вручную. Сертификаты, токен Proxmox, storage, сеть, VM/LXC и VPS updater не меняются. Сам updater также обновляется вручную после проверки нового выпуска.

Исправление после выпуска 0.9.0: venv первоначально создавался во временном каталоге `.incoming-*`, затем переносился в каталог релиза. Команды из `venv/bin` сохраняли shebang на старый путь и не запускались, хотя `python -m ...` и служба агента работали. Обновлённый updater после переноса проверяет запускаемые команды и при необходимости переустанавливает wheel из локального проверенного файла уже по окончательному пути. Updater на узле обновляется вручную, поэтому для существующего выпуска 0.9.0 его запускатели нужно один раз восстановить отдельно. Ни одна из этих операций не применяет nftables-правила и не включает допуск машин.

## Первая установка

Текущий рабочий агент уже установлен в `/opt/lab-manager-node/current`. На Proxmox под root скачайте **в отдельный каталог** все assets проверенного CI-релиза. `RELEASE_SHA` — полный SHA коммита из `dev-proxmox`; `MANIFEST_SHA256` — digest asset `SHA256SUMS` из GitHub release API. Не подставляйте значение хеша из самого скачанного манифеста.

```bash
set -euo pipefail
umask 077
release='<RELEASE_SHA>'
manifest_sha256='<MANIFEST_SHA256>'
wheel='<EXACT_WHEEL_NAME>'
stage="/opt/lab-manager-node/releases/$release"
test ! -e "$stage"
install -d -m 0700 "$stage"
cd "$stage"
for asset in "$wheel" \
             lab-node-agent.service lab-node-storage-snapshot.service \
             lab-node-storage-snapshot.timer node-pki.py \
             update-node.py install-node-updater.sh \
             lab-node-update.service lab-node-update.timer SHA256SUMS; do
  curl --fail --location --retry 3 --connect-timeout 15 --max-time 180 \
    --proto '=https' --proto-redir '=https' -o "$asset" \
    "https://github.com/<OWNER>/<REPO>/releases/download/node-agent-$release/$asset"
done
printf '%s  %s\n' "$manifest_sha256" SHA256SUMS | sha256sum --check --strict
sha256sum --check --strict SHA256SUMS
bash install-node-updater.sh "$release" </dev/null
```

В команде установки намеренно оставлены параметры релиза и репозитория: готовую команду с проверенными значениями следует брать из конкретного выпуска, а не копировать placeholders. Скрипт устанавливает проверенный wheel в отдельный venv, принимает текущий здоровый выпуск как baseline, ставит updater и timer. Первый запуск обновления выполняется в фоне. Возможен краткий перезапуск read-only агента; находящиеся на дисках гостевые данные не затрагиваются.

## Проверка и эксплуатация

```bash
systemctl list-timers --all lab-node-update.timer --no-pager
systemctl show --no-pager lab-node-update.service -p Result -p ExecMainStatus
journalctl -u lab-node-update.service -n 30 --no-pager
python3 -c 'import json; print(json.load(open("/opt/lab-manager-node/update-state/state.json"))["active_sha"])'
readlink -f /opt/lab-manager-node/current
systemctl is-active lab-node-agent.service lab-node-storage-snapshot.timer
```

`inactive (dead)` для oneshot нормально; успех определяется `Result=success` и `ExecMainStatus=0`. Для немедленной проверки используйте `systemctl start lab-node-update.service`. При отсутствии нового выпуска состояние не меняется. После обновления подтвердите на VPS свежий mTLS-снимок узла, поскольку локальная проверка сервиса не доказывает доставку данных на VPS.

На VPS из каталога репозитория:

```bash
docker compose --env-file .env.vps -f infra/vps/compose.yml exec -T worker \
  /app/.venv/bin/python -c '
from lab_manager.node_transport import load_endpoints, fetch
data, _ = fetch(load_endpoints("/run/lab-node-transport/nodes.json")[0])
sample = data["sample"]
print("Узел:", data["node_id"])
print("Снимок:", sample["sample_finished_at"])
print("Хранилища:", [p["storage"] for p in sample["local_thin_pools"]])
print("Допуск машин:", sample["admission_ready"])
'
```

Перед сменой версии updater сохраняет `pending.json`. Если новый агент не запустился или не записал читаемый снимок, он возвращает прежний symlink, перезапускает старую службу и блокирует неудачный SHA. При прерывании процесса следующий запуск разбирает pending-состояние; неизвестное состояние оставляется для ручного восстановления. Updater не удаляет старые releases и не выполняет автоматическую очистку диска. Не удаляйте текущий или предыдущий каталог выпуска.

Первая установка подтверждена на Proxmox для выпуска `edc3f88ffee4f7476a3575dc6a0f275cbbfc3b67`: внешний digest `SHA256SUMS` и все файлы релиза прошли проверку, wheel установился в отдельный venv, рабочий выпуск `ec27a8d5121fc53f5237239e344020342a8a01d5` принят как исходный, таймер включён. После фонового обновления активный SHA стал `edc3f88ffee4f7476a3575dc6a0f275cbbfc3b67`; служба updater завершилась с `Result=success`, `ExecMainStatus=0`, агент и таймер снимков активны. VPS worker после обновления успешно получил свежий снимок по mTLS с обоими thin-хранилищами (`local-lvm` и `student-lvm`). Допуск учебных машин остаётся `false` до завершения проверок вместимости и сети.

03.10.2026 на Proxmox подтверждено восстановление запускаемых команд выпуска `b44c95c47f6c49486a42f5ca279fe4646c5c72eb`. Предыдущая проверка обнаружила ошибку `lab-node-policy: cannot execute: required file not found`: команда существовала, но её shebang ссылался на удалённый временный каталог. В следующем запуске пользователь проверил активный SHA и отсутствие pending-обновления, манифест релиза, внешний digest файла updater, локально переустановил тот же wheel `0.9.0` по конечному пути и установил исправленный updater. Затем `lab-node-policy --help`, активность агента и deny-only guard, наличие обеих nftables-таблиц, `nft --check` с двумя режимами и проверка правил завершились сообщением `PASS`. Правила были только сформированы и проверены, но не применены.

Успешная процедура для этого конкретного выпуска (root shell; без адресов и секретов):

```bash
set -euo pipefail
release='b44c95c47f6c49486a42f5ca279fe4646c5c72eb'
stage="/opt/lab-manager-node/releases/$release"
systemctl start lab-node-update.service
test "$(python3 -c 'import json; print(json.load(open("/opt/lab-manager-node/update-state/state.json"))["active_sha"])')" = "$release"
test ! -e /opt/lab-manager-node/update-state/pending.json
test -d "$stage"
systemctl stop lab-node-update.timer
trap 'systemctl start lab-node-update.timer' EXIT
exec 9>/opt/lab-manager-node/update-state/update.lock
flock -n 9
(cd "$stage" && sha256sum --check --strict SHA256SUMS >/dev/null)
printf '%s  %s\n' \
  '3be2be3d267e5493f34242ea345bf5e00a05769110d8e0f61366703c66351298' \
  "$stage/update-node.py" | sha256sum --check --strict
"$stage/venv/bin/python" -m pip --disable-pip-version-check install \
  --no-index --no-deps --force-reinstall \
  "$stage/lab_node_agent-0.9.0-py3-none-any.whl"
install -o root -g root -m 0644 "$stage/update-node.py" \
  /usr/local/lib/lab-manager-node/update-node.py
policy="$stage/venv/bin/lab-node-policy"
"$policy" --help >/dev/null
systemctl is-active --quiet lab-node-agent.service lab-node-network-guard.service
nft list table inet lab_manager >/dev/null
nft list table bridge lab_manager_l2 >/dev/null
config=$(mktemp)
rules=$(mktemp)
trap 'rm -f "$config" "$rules"; systemctl start lab-node-update.timer' EXIT
printf '[{"bridge":"lmbrgroup1","mode":"GROUP_LAN"},{"bridge":"lmbrsolo1","mode":"ISOLATED"}]\n' > "$config"
"$policy" "$config" > "$rules"
nft --check --file "$rules"
grep -Fq 'meta ibrname "lmbrgroup1" accept' "$rules"
! grep -Fq 'meta ibrname "lmbrsolo1" accept' "$rules"
echo 'PASS: запускатель восстановлен, updater обновлён, оба режима проверены'
```

Процедура историческая и привязана к указанному SHA. Для следующих обновлений исправленный updater сам проверяет запускатели после переноса venv; повторять `pip --force-reinstall` вручную не требуется.

04.10.2026 подтверждено восстановление после неудачного обновления `d6efeec22b03b51dd16dbfe3fabc4f6900523f5e`. Из-за `UMask=0077` переустановка wheel в конечном каталоге оставляла часть пакета недоступной пользователю `lab-node-agent`. Updater вернул прежний выпуск `b44c95c47f6c49486a42f5ca279fe4646c5c72eb` и заблокировал неудачный SHA; работающий агент и таймер снимков сохранились. Исправленный выпуск `1a8099429c4cde710fa3088467f5cdb03a0c5ac7` прошёл CI. Он возвращает права чтения после переустановки wheel, в том числе при повторной попытке использовать уже подготовленный каталог, и проверяет импорт пакета от имени `lab-node-agent` до переключения текущего выпуска.

На Proxmox под root успешно выполнена следующая процедура. В ней нет адресов инфраструктуры или секретов; тестовая подсеть используется только как запись для пустого выключенного моста. SHA выпуска и digest относятся только к этому случаю. Блокировка исправленного SHA снималась лишь в случае, если прежний updater успел ошибочно заблокировать именно его.

```bash
bash <<'SH'
set -euo pipefail
test "$(id -u)" -eq 0

release='1a8099429c4cde710fa3088467f5cdb03a0c5ac7'
previous='b44c95c47f6c49486a42f5ca279fe4646c5c72eb'
digest='8e28f7e7de46751ad44d372ab0466a99f49befe8dc40266e236c2e8d0e53f68c'
tmp=$(mktemp)
spec=''
allocation_id=''
segment=''

cleanup() {
    rc=$?
    if [[ -n "$allocation_id" && -x "$segment" ]]; then
        "$segment" delete "$allocation_id" >/dev/null || true
    fi
    rm -f "$tmp"
    if [[ -n "$spec" ]]; then rm -f "$spec"; fi
    systemctl start lab-node-update.timer || rc=1
    exit "$rc"
}
trap cleanup EXIT

systemctl stop lab-node-update.timer
if systemctl is-active --quiet lab-node-update.service; then
    echo 'Updater ещё выполняется'
    exit 1
fi
test "$(readlink -f /opt/lab-manager-node/current)" = \
    "/opt/lab-manager-node/releases/$previous"
test ! -e /opt/lab-manager-node/update-state/pending.json

curl --fail --silent --show-error --location --retry 3 \
    --proto '=https' --proto-redir '=https' \
    -o "$tmp" \
    "https://github.com/Egoryich/lab-manager/releases/download/node-agent-$release/update-node.py"
printf '%s  %s\n' "$digest" "$tmp" | sha256sum --check --strict
install -o root -g root -m 0644 "$tmp" \
    /usr/local/lib/lab-manager-node/update-node.py

python3 - "$previous" "$release" <<'PY'
import json
import os
import pathlib
import sys
import tempfile

root = pathlib.Path('/opt/lab-manager-node/update-state')
old, candidate = sys.argv[1:]
state = json.loads((root / 'state.json').read_text())
assert state['active_sha'] == old
assert state.get('blocked_sha') in {
    None,
    'd6efeec22b03b51dd16dbfe3fabc4f6900523f5e',
    '4ba893f4f25984ec50d48ec99157b3eb6c4d2a46',
    candidate,
}
assert not (root / 'pending.json').exists()
if state.get('blocked_sha') == candidate:
    del state['blocked_sha']
    fd, name = tempfile.mkstemp(prefix='state-retry.', dir=root)
    with os.fdopen(fd, 'w') as stream:
        json.dump(state, stream, sort_keys=True)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(name, root / 'state.json')
    print('Блокировка исправленного выпуска снята')
PY

systemctl start lab-node-update.service
test "$(python3 -c 'import json; print(json.load(open("/opt/lab-manager-node/update-state/state.json"))["active_sha"])')" = "$release"
test ! -e /opt/lab-manager-node/update-state/pending.json
systemctl is-active --quiet lab-node-agent.service lab-node-storage-snapshot.timer

segment=/opt/lab-manager-node/current/venv/bin/lab-node-segment
allocation_id=$(python3 -c 'import uuid; print(uuid.uuid4())')
spec=$(mktemp)
printf '{"allocation_id":"%s","mode":"ISOLATED","cidr":"10.70.255.252/30"}\n' \
    "$allocation_id" > "$spec"
bridge=$("$segment" create "$spec")
ip -j -d link show dev "$bridge" |
    python3 -c 'import json,sys; x=json.load(sys.stdin)[0]; assert x["linkinfo"]["info_kind"]=="bridge" and "UP" not in x["flags"]; print("Пустой мост:", x["ifname"])'
"$segment" delete "$allocation_id"
allocation_id=''
test ! -e "/sys/class/net/$bridge"
systemctl show --no-pager lab-node-update.service -p Result -p ExecMainStatus
echo 'PASS: новый агент работает; тестовый мост создан и удалён'
SH
```

Проверенный результат: digest совпал, пустой мост `lmbrkstvgq` был удалён (`DELETED`), updater завершился с `Result=success` и `ExecMainStatus=0`, итоговая строка `PASS`. Тест не включал мост, не назначал ему адрес и не подключал гостевые машины. Временный тестовый сегмент удалён.

## Выпуск с адаптером Debian LXC

04.10.2026 пользователь проверил автоматическое обновление Proxmox-агента после успешного Linux CI: `lab-node-update.service` завершился с `Result=success`, `ExecMainStatus=0`, `active_sha` совпал с проверенным выпуском, установленная версия пакета — `0.11.0`. В этой версии есть только типизированный адаптер операций с LXC; он ещё не подключён к сетевому API и не создаёт машины без отдельного вызова. Успешная проверка выпуска без привязки к адресу или секретам:

```bash
systemctl start lab-node-update.service
systemctl show --no-pager lab-node-update.service -p Result -p ExecMainStatus
python3 -c 'import json; print(json.load(open("/opt/lab-manager-node/update-state/state.json"))["active_sha"])'
/opt/lab-manager-node/current/venv/bin/python -c \
  'from importlib.metadata import version; print(version("lab-node-agent"))'
```

## Обновление самого updater без переустановки агента

09.10.2026 исправленный `tools/update-node.py` был установлен на Proxmox отдельно от выпуска агента. Пользователь подтвердил совпадение SHA-256 обоих загруженных файлов и строку `PASS: verified node updater installed; timer restored by cleanup`. Эта операция не меняет активный выпуск агента; после неё обычный таймер может принять следующий проверенный выпуск.

Для повторения используйте полный SHA коммита и заранее проверенные SHA-256 скрипта установки и `update-node.py`. Не подставляйте хеши из непроверенного источника:

```bash
release='<FULL_VERIFIED_COMMIT_SHA>'
script_sha256='<VERIFIED_INSTALL_SCRIPT_SHA256>'
updater_sha256='<VERIFIED_UPDATER_SHA256>'
curl -fsSLo /tmp/lm-upgrade-node-updater.sh "https://raw.githubusercontent.com/Egoryich/lab-manager/$release/tools/upgrade-node-updater.sh"
printf '%s  %s\n' "$script_sha256" /tmp/lm-upgrade-node-updater.sh | sha256sum --check --strict
bash /tmp/lm-upgrade-node-updater.sh "$release" "$updater_sha256"
```

После публикации проверенного выпуска агент `0.16.0` был установлен штатным `lab-node-update.service`. Пользователь подтвердил `Result=success`, `ExecMainStatus=0`, совпадение `active_sha` с опубликованным выпуском и состояние `active` для агента, помощника сегментов, сетевого предохранителя и таймера. Короткая проверка после каждого обновления:

```bash
systemctl start lab-node-update.service
systemctl show --no-pager lab-node-update.service -p Result -p ExecMainStatus
python3 -c 'import json; print(json.load(open("/opt/lab-manager-node/update-state/state.json"))["active_sha"])'
/opt/lab-manager-node/current/venv/bin/python -c 'from importlib.metadata import version; print(version("lab-node-agent"))'
systemctl is-active lab-node-agent.service lab-node-segment-helper.service lab-node-network-guard.service lab-node-update.timer
```
