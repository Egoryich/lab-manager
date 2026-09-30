# Автообновление агента Proxmox

После однократной установки `lab-node-update.timer` примерно раз в пять минут проверяет `dev-proxmox` исходящим HTTPS-запросом. GitHub не подключается к узлу по SSH. VPS обновляется отдельным таймером из `dev-vps`; ветки и механизмы доставки независимы.

Updater принимает только новый потомок активного коммита, если последний push-run `check.yml` для точного SHA завершился успешно и jobs `identity-groups`, `node-agent`, `node-agent-release` имеют `success`. Релиз `node-agent-<SHA>` обязан указывать на этот коммит. Все скачанные файлы проверяются по SHA-256, опубликованным GitHub для assets, и дополнительно по `SHA256SUMS`.

Автоматически заменяется только wheel агента. Если `lab-node-agent.service` или storage snapshot service/timer изменились, updater останавливается до переключения: такой выпуск ставится вручную. Сертификаты, токен Proxmox, storage, сеть, VM/LXC и VPS updater не меняются. Сам updater также обновляется вручную после проверки нового выпуска.

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

Перед сменой версии updater сохраняет `pending.json`. Если новый агент не запустился или не записал читаемый снимок, он возвращает прежний symlink, перезапускает старую службу и блокирует неудачный SHA. При прерывании процесса следующий запуск разбирает pending-состояние; неизвестное состояние оставляется для ручного восстановления. Updater не удаляет старые releases и не выполняет автоматическую очистку диска. Не удаляйте текущий или предыдущий каталог выпуска.

Команда первой установки и её результат помечаются подтверждёнными только после фактического вывода с Proxmox. До этого руководство описывает подготовленную процедуру.
