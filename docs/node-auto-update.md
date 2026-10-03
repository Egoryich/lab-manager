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
