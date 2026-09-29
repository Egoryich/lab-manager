# Proxmox: первая поставка компонента инвентаризации

Ветка `dev-proxmox` продолжает проверенную общую основу VPS. Изменения этой ветки не публикуют VPS-образы и не забираются VPS-updater. `main` пока не изменён. Цель первого шага — проверить чтение реального Proxmox API отдельным сервисным identity перед подключением удалённого агента и управления гостями.

## Что реализовано

`apps/node-agent` — отдельный Python 3.13+ пакет `lab-node-agent`. Runtime-зависимостей нет; тестовые зависимости закреплены отдельным `uv.lock`. CLI выполняет один сбор и выводит нормализованный JSON. API token читается из локального JSON-файла, не передаётся аргументом командной строки. На Linux запрещён файл credentials с доступом для остальных пользователей; рекомендуемые права — 0600, владелец будущий service user.

Разрешены только GET к `/version`, `/access/permissions?path=/`, `/nodes/{configured-node}/status`, `/storage`, `/qemu`, `/lxc` относительно настроенного HTTPS origin. Сегмент node проходит проверку формата. Произвольный API path, URL или метод не принимаются. Проверка CA/hostname обязательна, redirects запрещены, proxy environment игнорируется, ошибки не возвращают provider body или токен. Ответ ограничен 4 MiB, socket timeout 5 секунд. Последовательный сбор не является атомарным снимком.

Минимальный набор чтения для этой поставки: `Sys.Audit`, `VM.Audit`, `Datastore.Audit` на `/` у отдельного пользователя и privilege-separated token. Нужны отрицательные проверки на целевом PVE: без соответствующих прав списки могут быть отфильтрованы. Широкая область чтения нужна для учёта внешних потребителей; прав изменения нет. Матрица основана на [Proxmox API viewer](https://pve.proxmox.com/pve-docs/api-viewer/) и [официальной модели permissions/token](https://github.com/proxmox/pve-docs/blob/master/pveum.adoc). Создание identity и запись секрета будут отдельным шагом после проверки хоста; пароль/токен в чат не передаются.

Все гости получают `ownership=UNVERIFIED`, включая пользовательские тестовые LXC. `maxdisk` сохраняется как `reported_maxdisk_bytes`: это не сумма всех дисков и не база для квоты. Неизвестная величина остаётся null, а не 0. Thin metadata, дисковые обязательства, ownership, сети и gateway ещё не сверяются, поэтому `admission_ready=false` всегда. Глобальные audit grants проверяются, но это не доказывает отсутствие ограничивающих ACL ниже `/`. Недостаточные права приводят к отказу, а не к «свободному серверу».

## Проверки и поставка

Отдельный CI job `node-agent` проверяет код, запускает восемь тестов, собирает wheel и проверяет его установку в чистый venv без зависимостей. Для push в `dev-proxmox` после успеха общих и agent-проверок создаётся GitHub prerelease `node-agent-<полный SHA>` с wheel и `SHA256SUMS`. Повторный CI не перезаписывает существующие release assets. Это CI и публикация пакета, ещё не автоматическая установка на Proxmox.

Протестированы HTTPS с локальным тестовым сервером, проверка hostname сертификата, запрет redirects и чужих путей, ограничение ответа и сокрытие ошибок, отказ узкому token, отсутствие ложного admission, сохранение внешних гостей и отказ при противоречивых списках. Эти тесты не подменяют реальный Proxmox или проверку его ACL.

## Первый шаг на физическом Proxmox

Команды выполняет пользователь в административном shell **Proxmox**, не VPS. Они читают hostname, версию Python, состояние служб и публичную часть сертификата; не устанавливают пакеты и не меняют гостей. Фактические имена и адреса из ответа сохраняются только в локальный inventory.

```bash
hostname
hostname -f
python3 --version
pvesh get /nodes --output-format json
getent hosts "$(hostname -f)"
systemctl is-active pveproxy tailscaled
ls -l /etc/pve/pve-root-ca.pem
cert=/etc/pve/local/pve-ssl.pem
if [ -f /etc/pve/local/pveproxy-ssl.pem ]; then
    cert=/etc/pve/local/pveproxy-ssl.pem
fi
openssl x509 -in "$cert" -noout -subject -issuer -dates -ext subjectAltName
```

Ожидается имя node, Python 3.13+, активные pveproxy/tailscaled и сертификат с подходящим SAN. При установленном пользовательском/ACME-сертификате `pve-root-ca.pem` может не быть нужным trust anchor — это определяется по выводу. Нельзя исправлять несовпадение сертификата отключением TLS-проверки.

29.09.2026 пользователь подтвердил выполнение блока в root shell Proxmox: Python 3.13.5, узел online, 24 логических CPU, обе службы active, CA-файл существует. Публичный сертификат выпущен PVE Cluster Manager CA, действует до сентября 2028 года, SAN включает localhost и IPv4 loopback. Фактические имена/IP сохранены только в игнорируемом локальном inventory. Это ещё не проверка TLS handshake или доступа API token.

## Создание доступа только для чтения

Следующий блок выполняется один раз в root shell Proxmox после предыдущей проверки. Его успешное выполнение подтверждено пользователем 29.09.2026 (подробности ниже). Loopback допустим только при наличии его IP в SAN, как в подтверждённом выше выводе. `openssl` проверяет доверие цепочке и IP перед созданием identity. Команды `pveum` сверены с [официальным справочником](https://raw.githubusercontent.com/proxmox/pve-docs/master/generated/pveum.1-synopsis.adoc).

Создаются отдельная роль, пользователь без пароля и privilege-separated token. Секрет сохраняется в root-only каталоге, не выводится в терминал. При совпадении существующих имён команда создания завершится ошибкой; не удалять существующие объекты и не запускать последующие строки отдельно. При частичном выполнении сохранить вывод для разбора. Файл `token-created.json` на случай ошибки преобразования сохраняет единственный выданный секрет.

```bash
bash <<'SH'
set -euo pipefail
umask 077
test "$(id -u)" -eq 0

openssl s_client -connect 127.0.0.1:8006 \
  -CAfile /etc/pve/pve-root-ca.pem \
  -verify_ip 127.0.0.1 -verify_return_error -brief </dev/null

install -d -m 700 /etc/lab-manager
test ! -e /etc/lab-manager/proxmox-token.json
test ! -e /etc/lab-manager/token-created.json

pveum role add LabInventory --privs 'Sys.Audit VM.Audit Datastore.Audit' </dev/null
pveum user add lab-inventory@pve --comment 'Lab Manager read-only inventory' </dev/null
pveum acl modify / --users lab-inventory@pve --roles LabInventory --propagate 1 </dev/null

( set -C
  pveum user token add lab-inventory@pve inventory --privsep 1 --output-format json \
    </dev/null > /etc/lab-manager/token-created.json
)

python3 <<'PY'
import json
import os
from pathlib import Path
raw = Path('/etc/lab-manager/token-created.json')
data = json.loads(raw.read_text())
assert data['full-tokenid'] == 'lab-inventory@pve!inventory'
assert isinstance(data['value'], str) and data['value']
with Path('/etc/lab-manager/proxmox-token.json').open('x') as output:
    json.dump({'token_id': data['full-tokenid'], 'token_secret': data['value']}, output)
    output.flush()
    os.fsync(output.fileno())
raw.unlink()
print('Credentials saved; secret not displayed.')
PY

pveum acl modify / --tokens 'lab-inventory@pve!inventory' --roles LabInventory --propagate 1 </dev/null
pveum user token permissions lab-inventory@pve inventory --path / --output-format json </dev/null
echo 'Read-only access prepared'
SH
```

Ожидаются успешная проверка сертификата, сообщение о сохранении credentials и три audit-права. Последний запрос показывает только права, не секрет. Пакет и systemd-служба этим блоком не устанавливаются. Следующий шаг — установка опубликованного wheel и реальный API-запрос этим токеном.

Подтверждение 29.09.2026: пользователь прислал `Verification: OK` (TLS 1.3), `Credentials saved; secret not displayed.`, ровно `Datastore.Audit`, `Sys.Audit`, `VM.Audit` на `/` и `Read-only access prepared`. Сообщение `Can't use SSL_get_servername` при подключении по IP не отменяет успешную проверку IP/цепочки. Секрет не передавался. Это проверка TLS и ACL через административный CLI, ещё не фактическая аутентификация токеном через REST.

## Установка пакета и первый API-снимок

Успешное выполнение следующего блока подтверждено пользователем 29.09.2026. Запуск в root shell Proxmox. Устанавливаются только curl и поддержка venv (без общего upgrade ОС); пакет помещается в отдельный каталог выпуска и не меняет системный Python. SHA-256 wheel проверен по опубликованному asset GitHub release. Репозиторий и Git на узле не требуются. Однократный диагностический запуск выполняется root; постоянная служба с отдельным системным пользователем будет следующим этапом.

```bash
bash <<'SH'
set -euo pipefail
umask 077
test "$(id -u)" -eq 0
test -s /etc/lab-manager/proxmox-token.json

apt-get update </dev/null
apt-get install -y --no-install-recommends python3-venv curl </dev/null

release='0a749e6d532bfaef627f14743b0959a3538d210c'
wheel='lab_node_agent-0.1.0-py3-none-any.whl'
base="/opt/lab-manager-node/releases/$release"
install -d -m 700 "$base" /var/lib/lab-manager-node
cd "$base"

curl --fail --location --retry 3 --connect-timeout 15 --max-time 180 \
  --proto '=https' --proto-redir '=https' \
  -o "$wheel" \
  "https://github.com/Egoryich/lab-manager/releases/download/node-agent-$release/$wheel"
printf '%s  %s\n' \
  '718538b7cc6e4d4ee94ae52808dcfb00479b33f4d15cd8ab760a3b99384fbf97' \
  "$wheel" | sha256sum --check --strict

python3 -m venv "$base/venv" </dev/null
"$base/venv/bin/python" -m pip --disable-pip-version-check install \
  --no-index --no-deps "$base/$wheel" </dev/null

snapshot=$(mktemp /var/lib/lab-manager-node/inventory.XXXXXX)
"$base/venv/bin/lab-node-agent" \
  --origin https://127.0.0.1:8006 \
  --node "$(hostname -s)" \
  --ca-file /etc/pve/pve-root-ca.pem \
  --credentials-file /etc/lab-manager/proxmox-token.json \
  > "$snapshot" </dev/null
mv "$snapshot" /var/lib/lab-manager-node/inventory.json
cat /var/lib/lab-manager-node/inventory.json
echo 'Inventory collected successfully'
SH
```

При ошибке apt/TLS/пакета остановиться и передать вывод; не отключать проверку TLS и не менять Proxmox repositories наугад. На успехе JSON содержит CPU/RAM, storage и гостей без токена. `admission_ready=false` и `ownership=UNVERIFIED` ожидаемы. Файл последнего успешного снимка заменяется только после успешного сбора. На этом этапе снимок ещё не отправляется на VPS.

## Последующие шаги

1. Подтвердить имя node и доверие TLS, создать отдельный read-only identity/token и файл credentials на node.
2. Установить wheel проверенного SHA в выделенный venv, выполнить инвентаризацию и сравнить с ранее полученным CLI-выводом. Только после ответа пользователя отметить команды выполненными.
3. Добавить службу агента, mTLS и привязку node identity, приём снимков на VPS с проверкой свежести/полноты. Служба не будет работать от root без необходимости.
4. Настроить отдельный pull-updater для `dev-proxmox` после проверки службы. Guacamole, управление гостями и сетью выполняются последующими этапами; существующие тестовые LXC не удаляются и не импортируются автоматически.

## Подтверждение первого API-снимка — 29 сентября 2026

Источник: полный вывод команд, присланный пользователем. Установлены четыре пакета поддержки venv; существующие пакеты ОС не обновлялись. SHA-256 wheel совпал, `lab-node-agent 0.1.0` установлен в отдельный venv. CLI завершился сообщением `Inventory collected successfully`. Аутентификация отдельным токеном и чтение реального REST API подтверждены.

Получены версия PVE 9.2.20, 24 логических CPU / 12 cores / 1 socket, 33 547 112 448 байт RAM, три активных storage и два остановленных внешних LXC, что согласуется с прежним пользовательским инвентарём. Сбор занял около 0.13 секунды. Имена, адреса и полный снимок сохранены только в локальном игнорируемом inventory.

`admission_ready=false`, `ownership=UNVERIFIED` и неизвестная thin metadata ожидаемы для этой версии. Свободное место thin pool не учитывает все обязательства по виртуальным дискам и не является гарантией вместимости занятий. Подтверждён однократный запуск от root; системная служба, запуск от отдельного пользователя, передача на VPS и автоматические обновления пока не установлены.
