# Постоянная инвентаризация Proxmox через mTLS

Этот выпуск связывает физический Proxmox и VPS через существующий Headscale/Tailscale. На Proxmox отдельная systemd-служба от непривилегированного пользователя каждые 30 секунд читает Proxmox API уже созданным read-only token. Она слушает только указанный адрес tailnet и отдаёт только `GET /v1/inventory`. VPS worker раз в 30 секунд забирает снимок, проверяет CA, имя адреса в сертификате, SHA-256 серверного сертификата, клиентский сертификат, идентификатор узла, возраст и схему данных. Успешный снимок и последний код ошибки хранятся в PostgreSQL; администратор видит состояние на странице «Серверы».

Ни один снимок не является разрешением на создание машин: `admission_ready=false` неизменно. Если Proxmox выключен или недоступен, последняя запись остаётся, но становится `STALE`; это не означает достоверно `OFF`. Команд запуска, удаления, сетевых изменений, SSH и пользовательского доступа к порту агента нет. Для постоянной работы Proxmox должен быть включён; Tuya-управление питанием — отдельный канал.

Здесь описан полный ручной путь. Команды с фактическими адресами, путями и SHA конкретной установки отмечаются выполненными только после вывода пользователя. Выпуск изменяет миграцию и Compose, поэтому действующий VPS updater остановится на нём и нужен ручной переход. `dev-proxmox` выпускает wheel и unit; тот же проверенный исходный коммит переносится в `dev-vps` для GHCR-образов. `main` принимается только после проверки обеих частей.

## Проверки до установки

На VPS должно оставаться достаточно диска для двух новых образов и миграции. Существующие пять контейнеров и публичный HTTPS должны быть исправны. На Proxmox должны работать `tailscaled`, `pveproxy`, уже сохранённый read-only token и CLI 0.1.0. Фактический `tailscale ip -4` узла должен совпадать с адресом, который попадёт в серверный сертификат. Нужны разрешённые соединения VPS → Proxmox к порту агента внутри tailnet. Внешний публичный интерфейс Proxmox не используется.

Прежде чем выдавать новые команды, получить полный SHA выпуска из успешного GitHub Actions `dev-proxmox` и сверить assets `node-agent-<SHA>` с wheel, `SHA256SUMS`, `lab-node-agent.service`, `node-pki.py`. Проверка `sha256sum -c SHA256SUMS` выполняется в каталоге, содержащем эти четыре файла. Не копировать приватные ключи между узлами.

## Создать закрытый ключ и запрос на Proxmox

На Proxmox, root shell. `RELEASE_SHA` — проверенный полный SHA. Это подготовка: запущенную службу и текущий CLI не меняет. Загрузить четыре asset из GitHub release; указанная проверка охватывает весь набор. Каталог TLS должен быть пустым; повторный запуск при уже существующем ключе остановится.

```bash
set -euo pipefail
umask 077
RELEASE_SHA='ПОЛНЫЙ_SHA_ИЗ_УСПЕШНОГО_CI'
stage="/opt/lab-manager-node/releases/$RELEASE_SHA"
install -d -m 0700 "$stage"
cd "$stage"
for asset in lab_node_agent-0.2.0-py3-none-any.whl lab-node-agent.service node-pki.py SHA256SUMS; do
    curl --fail --location --proto '=https' --proto-redir '=https' \
      --retry 3 -o "$asset" \
      "https://github.com/Egoryich/lab-manager/releases/download/node-agent-$RELEASE_SHA/$asset"
done
sha256sum --check --strict SHA256SUMS
python3 node-pki.py node-init --directory /etc/lab-manager-node/tls
```

Сохранить выведенные `Node ID` и блок `-----BEGIN CERTIFICATE REQUEST-----` … `-----END CERTIFICATE REQUEST-----`. Это открытый запрос; файл `/etc/lab-manager-node/tls/server.key` и ранее созданный Proxmox API token не показывать и не передавать. Требуется именно полный CSR, без форматирования и лишних строк.

29.09.2026 пользователь подтвердил этот этап на Proxmox: все четыре asset выпуска `28208ca6c608d07117ff7d6f97838f1f1441610c` прошли проверку SHA-256, `node-init` вывел новый Node ID и полный открытый CSR. Успех относится только к загрузке пакета и созданию запроса; сертификат ещё не подписан, служба не установлена. Реальный ID/CSR хранятся в локальном игнорируемом inventory, не в публичной инструкции. Исполненный блок не печатал и не передавал закрытый ключ.

## Создать центр доверия на VPS и подписать CSR

Выполняется на VPS root после получения CSR. Код подписания тот же проверенный `node-pki.py` из выпуска. Если VPS checkout ещё на старом выпуске, получить его из уже проверенного Git commit командой `git show "$RELEASE_SHA:tools/node-pki.py" > /opt/lab-manager/node-pki.py` после `git fetch origin dev-proxmox` и проверки, что SHA является предком `origin/dev-proxmox`; публичную контрольную сумму сравнить с release asset. Отдельный каталог `/opt/lab-manager/transport-pki` хранит CA и клиентский закрытый ключ 0700. `control-init` одноразовый; при повторном вызове с существующими ключами откажет. CSR записывается в файл из дословно переданного публичного PEM. `NODE_ID` берётся из ответа Proxmox; `NODE_TAILNET_IP` проверяется на узле командой `tailscale ip -4`.

```bash
set -euo pipefail
umask 077
python3 /opt/lab-manager/node-pki.py control-init \
  --directory /opt/lab-manager/transport-pki
install -m 0600 /dev/null /opt/lab-manager/transport-pki/node.csr
cat > /opt/lab-manager/transport-pki/node.csr <<'CSR'
-----BEGIN CERTIFICATE REQUEST-----
ДОСЛОВНЫЙ_ПУБЛИЧНЫЙ_CSR
-----END CERTIFICATE REQUEST-----
CSR
python3 /opt/lab-manager/node-pki.py sign-node \
  --directory /opt/lab-manager/transport-pki \
  --csr /opt/lab-manager/transport-pki/node.csr \
  --node-id "$NODE_ID" --address "$NODE_TAILNET_IP"
```

Вывод `sign-node` — одна строка base64 с публичным bundle: CA, сертификат сервера, ID узла и отпечатки сертификатов. Приватные ключи в неё не включены. Её можно перенести на Proxmox. `node.csr` и `public-bundle.json` можно хранить для аудита; `ca.key` и `client.key` остаются только на VPS. Срок server/client сертификатов — 365 дней; до истечения потребуется плановая ротация.

29.09.2026 пользователь подтвердил выполнение `control-init` и `sign-node` на VPS для подготовленного узла: проверка SHA-256 `node-pki.py` прошла, новый CA/клиент созданы, подписанный публичный bundle выведен. Ассистент проверил по присланному bundle совпадение Node ID, адреса SAN, SHA-256 сертификата, цепочки issuer/subject и отсутствие закрытых ключей. Сертификат сервера действует до 29.09.2027 UTC. Это ещё не передача bundle на Proxmox и не проверка реального mTLS-соединения. Команды, выдавшие результат, сохранены выше без адресов конкретной установки.

## Установить постоянную службу на Proxmox

Этот блок выполняется после получения публичного bundle. Строго проверить ID, адрес и отпечатки до старта; файл bundle создаётся в `/etc/lab-manager-node/public-bundle.b64`. Для переноса через терминал на VPS использовать `base64 -w76 /opt/lab-manager/transport-pki/<node-id>/public-bundle.json` и вставить многострочный вывод в quoted heredoc на Proxmox. Исходная однострочная строка длиннее типичного лимита одной строки терминала, поэтому не читать её одним интерактивным `read`. При декодировании убрать только переводы строк. `NODE_TAILNET_IP` — адрес самого Proxmox, не VPS. Использовать только один проверенный выпуск и не перезаписывать текущий `server.key`.

```bash
set -euo pipefail
umask 077
RELEASE_SHA='ПОЛНЫЙ_SHA_ИЗ_УСПЕШНОГО_CI'
stage="/opt/lab-manager-node/releases/$RELEASE_SHA"
install -d -m 0755 /etc/lab-manager-node
cat > /etc/lab-manager-node/public-bundle.b64 <<'BUNDLE'
ОДНА_СТРОКА_ПУБЛИЧНОГО_BUNDLE
BUNDLE
python3 - <<'PY'
import base64, hashlib, json, os, pathlib, ssl, subprocess
directory = pathlib.Path('/etc/lab-manager-node')
tls = directory / 'tls'
encoded = ''.join((directory / 'public-bundle.b64').read_text().split())
bundle = json.loads(base64.b64decode(encoded, validate=True))
node_id = (tls / 'node-id').read_text().strip()
assert bundle['node_id'] == node_id
assert bundle['address'] == subprocess.check_output(['tailscale', 'ip', '-4'], text=True).strip()
assert bundle['client_sha256'] == bundle['client_sha256'].lower()
for name, content in [('ca.pem', bundle['ca_pem']), ('server.pem', bundle['server_pem'])]:
    with (tls / name).open('x') as output:
        output.write(content)
der = ssl.PEM_cert_to_DER_cert((tls / 'server.pem').read_text())
assert hashlib.sha256(der).hexdigest() == bundle['server_sha256']
subprocess.run(['openssl', 'verify', '-CAfile', str(tls / 'ca.pem'),
                '-verify_ip', bundle['address'], str(tls / 'server.pem')], check=True)
config = {'listen_address': bundle['address'], 'listen_port': 18443,
          'proxmox_origin': 'https://127.0.0.1:8006',
          'proxmox_node': subprocess.check_output(['hostname', '-s'], text=True).strip(),
          'node_id': node_id, 'client_sha256': bundle['client_sha256']}
with (directory / 'service.json').open('x') as output:
    json.dump(config, output)
os.chmod(directory / 'service.json', 0o644)
print('Public certificate and configuration verified.')
PY
python3 -m venv "$stage/venv" </dev/null
"$stage/venv/bin/python" -m pip --disable-pip-version-check install \
  --no-index --no-deps "$stage/lab_node_agent-0.2.0-py3-none-any.whl" </dev/null
chmod 0755 /opt/lab-manager-node /opt/lab-manager-node/releases "$stage"
chmod -R a+rX "$stage/venv"
id lab-node-agent >/dev/null 2>&1 || useradd --system --user-group \
  --no-create-home --shell /usr/sbin/nologin lab-node-agent
ln -s "$stage" /opt/lab-manager-node/current
install -o root -g root -m 0644 "$stage/lab-node-agent.service" \
  /etc/systemd/system/lab-node-agent.service
systemctl daemon-reload
systemctl enable --now lab-node-agent.service
systemctl is-active lab-node-agent.service
journalctl -u lab-node-agent.service -n 20 --no-pager
ss -lntup | grep 18443
```

Подтверждённый ранее CLI 0.1.0 остаётся в прежнем каталоге. systemd загружает API token и ключи как отдельные временные credentials для непривилегированного процесса; права исходных файлов не расширяются. Если служба не запустилась, сохранить journal, не отключать TLS. При повторной установке проверить существующий symlink/файлы и выполнить отдельную процедуру обновления, а не запускать блок с `open('x')` второй раз. Источник модели credentials: [systemd.exec](https://github.com/systemd/systemd/blob/main/man/systemd.exec.xml).

## Выпуск и настройка VPS

Общая база VPS должна перейти на `0004_nodes`, образы API/worker/web на одно и то же значение `dev-vps` SHA. Изменения Compose/схемы блокируют автоматический updater. Выполнять ручной переход по образцу [проверенного worker rollout](vps-worker-rollout.md): остановить timer, проверить pending/dirty checkout, сохранить старый `.env.vps`, скачать образы, под lock остановить API+worker, применить миграцию и запустить пять контейнеров, проверить локальный и публичный readiness, затем `--adopt` и переустановить timer. Не выполнять автоматический downgrade БД и не удалять volumes. Команды с точным SHA/фактическими образами записать в гайд после CI.

После установки образы ещё не опрашивают узел, пока worker не получит root-owned конфигурацию. Подготовить на VPS `/opt/lab-manager/node-transport` с `ca.pem`, `client.pem`, `client.key` из `/opt/lab-manager/transport-pki` и `nodes.json` со стабильным node ID/адресом/отпечатком из `public-bundle.json`. Путь смонтировать только в worker через `LAB_NODE_TRANSPORT_DIR=/opt/lab-manager/node-transport`; `LAB_NODE_CONFIG=/run/lab-node-transport/nodes.json`. UID 10001 контейнера должен читать клиентский ключ, но никто другой; CA private key сюда не копировать. Затем пересоздать только worker, посмотреть его journal, вызвать `/api/admin/nodes` под Admin и проверить страницу «Серверы». Достижимость из контейнера до tailnet адреса проверить до объявления этапа завершённым: сеть Compose может требовать маршрут/Docker firewall через host.

Схема `nodes.json`:

```json
[
  {
    "id": "СТАБИЛЬНЫЙ_UUID_УЗЛА",
    "name": "Учебный сервер",
    "origin": "https://АДРЕС_TAILNET_УЗЛА:18443",
    "node": "pve",
    "server_sha256": "ОТПЕЧАТОК_ИЗ_PUBLIC_BUNDLE",
    "ca": "/run/lab-node-transport/ca.pem",
    "certificate": "/run/lab-node-transport/client.pem",
    "key": "/run/lab-node-transport/client.key"
  }
]
```

Это пока ручная доставка двух выпусков. Обновлятор Proxmox и автоматическая ротация сертификатов ещё не готовы; журнал подтверждённых команд дополняется после каждого реального шага. Ограничения исходного inventory по thin metadata, дисковым обязательствам и ACL остаются; статья [первой установки](proxmox-agent-first-install.md) содержит подтверждённый одноразовый запуск.
