# Автообновление VPS без SSH из GitHub

После установки и включения на VPS работает systemd timer: через две минуты после загрузки и затем примерно каждые пять минут запускается проверка. GitHub не подключается к серверу. Updater использует исходящие HTTPS-запросы к публичному репозиторию и GHCR; отдельные токены не нужны, если оба пакета доступны публично.

## Что обновляется автоматически

- API, web и worker из текущей вершины `dev-vps`. Применяется один и тот же Git SHA для образов, скачанные образы закрепляются по digest.
- Требуется успешный последний запуск `check.yml` именно для push этого SHA в `dev-vps`, включая jobs `identity-groups` и `vps-images`. Ошибки/незавершённые проверки не позволяют использовать более старый успешный запуск того же SHA.
- Новый SHA должен быть потомком установленного: force-push и откат ветки не приводят к автоматическому downgrade.
- Compose должен быть неизменным относительно активного выпуска. При изменении дерева миграций updater проверяет единственную целевую Alembic head, временно останавливает API/worker, создаёт и проверяет временный архив PostgreSQL, выполняет миграцию и проверяет новый выпуск. Изменение ресурсов/сетей контейнеров или структуры Compose требует отдельного ручного развёртывания.
- Updater проверяет отсутствие ручных изменений `.env.vps`, Compose, работающих образов и версии схемы. Перед pull требуется 1.5 GiB свободного места; после pull — 512 MiB. Автоматической очистки образов или томов нет.

Заменяются только API/web/worker с `--no-deps --pull never`; Headscale, Caddy, Tailscale, PostgreSQL и Redis не перезапускаются. При миграции API недоступен до проверки нового выпуска; это не zero-downtime deployment. Архив хранится в `/opt/lab-manager/update-state/migration.dump` с правами root и удаляется после успешного завершения либо подтверждённого восстановления. Регулярное резервное копирование этим не включается.

## Ошибки и восстановление

`flock` исключает параллельные запуски updater. Перед заменой сохраняются предыдущие точные image IDs, секреты и запись незавершённой операции. Проверяются локальные API/web, worker и публичный HTTPS readiness. При неудаче обычного обновления восстанавливаются прежние образы. При неудаче миграции **до запуска нового API** updater восстанавливает предыдущую БД из архива и прежние образы. Неудачный SHA блокируется до нового коммита или явного `--retry`.

Если процесс/сервер прерван, следующий запуск обнаруживает pending-запись. При миграции он завершает выпуск, только если новая схема и сервисы уже готовы. После начала запуска нового API он **не** восстанавливает старую БД автоматически: API мог принять записи, которые потерялись бы при откате. Сбой на этой стадии, неизвестная ревизия схемы, отсутствие исправного архива или ошибка восстановления оставляют pending-запись и архив для ручного разбора. Изменённый Compose также останавливает автоматическое восстановление.

Данные и сессии сохраняются в PostgreSQL. Предыдущие образы нужно сохранять на диске: не запускайте `docker image prune -a` или очистку томов во время эксплуатации updater. При достижении порога диска он останавливает обновление и пишет причину в journal. Для этого первого механизма уведомления во внешний канал не настроены; состояние проверяется через systemd/journal.

## Установка поверх первого VPS-запуска

Дождитесь успеха **всего** workflow выпуска updater. Используйте полный SHA вместо `VERIFIED_UPDATER_SHA`. Первоначальный checkout пока **не переключайте**: он должен совпадать с установленной версией из `.env.vps`.

```bash
cd /opt/lab-manager/repo
sudo git fetch origin dev-vps
# Сохранить проверенный установщик в файл, а не передавать shell через pipe.
installer=$(mktemp)
if sudo git show VERIFIED_UPDATER_SHA:tools/install-vps-updater.sh > "$installer"; then
    sudo bash "$installer" VERIFIED_UPDATER_SHA
fi
rm -f "$installer"
```

Установщик копирует updater и systemd units из заданного коммита, проверяет исходное состояние, включает timer и выполняет первую проверку/обновление. Секреты не переписываются новыми значениями. Updater установлен отдельно в `/usr/local/lib/lab-manager/update-vps.py` и не заменяет сам себя автоматически.

```bash
systemctl list-timers lab-manager-update.timer --no-pager
sudo journalctl -u lab-manager-update.service -n 30 --no-pager
curl --fail "https://${LAB_DOMAIN:?Set LAB_DOMAIN first}/api/health/ready"
```

Сервис имеет тип oneshot, поэтому `inactive (dead)` после успешного выполнения нормален; результат смотрите через `systemctl show --no-pager lab-manager-update.service -p Result -p ExecMainStatus`. Timer должен быть активен.

## Если после установки нет timer и журнал пуст

В первом установщике запуск через `git show … | bash` мог прерваться после `Verified compatible release`: неинтерактивный дочерний Docker-процесс наследовал stdin и забирал оставшиеся команды shell. Исправлено закрытием stdin дочерних процессов и запуском установщика из файла. Проверка `Verified compatible release` сама по себе не подтверждает установку timer. Повторите установку исправленной версии, сохранив checkout и `.env.vps`; приложение и данные переустанавливать не нужно. Общая диагностика: [единый гайд](deployment-guide.md).

## Эксплуатация

Подтверждено пользователем 28.09.2026 на VPS: установка исправленного updater из файла завершилась успешно; timer активен и имеет следующую дату запуска. Первый запуск установил выпуск `7e14b6b368bef2e306bb4e447cadae14090825d9`. В journal последовательно появились `Verified compatible release`, `Updated application to`, `Deactivated successfully` и `Finished`. Длительность запуска — около 22 секунд, peak memory service — 88.2M по systemd; это наблюдение одного обновления, не общий ресурсный бюджет приложения. Команды установки выше и проверки timer/journal подтверждены этим результатом. Hostname из вывода в гайд не переносится.

```bash
# Проверка кандидата без замены контейнеров. При первом запуске сохраняет baseline.
sudo python3 /usr/local/lib/lab-manager/update-vps.py --check

# Проверить и применить сейчас; использует тот же lock, что и timer.
sudo systemctl start lab-manager-update.service

# Приостановить будущие проверки; уже выполняющийся запуск нужно дождаться.
sudo systemctl disable --now lab-manager-update.timer

# После разбора неудачи разрешить повтор заблокированного SHA.
sudo python3 /usr/local/lib/lab-manager/update-vps.py --retry
```

`/opt/lab-manager/update-state/state.json` хранит активный SHA и метаданные. `previous.env` и `candidate.env` содержат секреты и доступны root; их не присылают в чат. Для ручных compose-команд актуальный файл остаётся `/opt/lab-manager/repo/.env.vps`. Checkout не переключается при автоматическом обновлении: установленную версию смотрите в `state.json`, а не в `git log`.

Для ручного выпуска с новым Compose отключите timer, дождитесь окончания service, выполните согласованные инструкции конкретного выпуска, переключите checkout на его SHA и задайте оба SHA-тега в `.env.vps`, сохранив секреты. После миграции и проверки выполните `sudo python3 /usr/local/lib/lab-manager/update-vps.py --adopt`, затем снова включите timer. Adopt проверяет совпадение checkout, env, работающих образов и readiness, но не заменяет проверку полного CI и совместимости вручную развёрнутого выпуска. При pending-операции adopt запрещён.

Источники: [GitHub workflow runs API](https://docs.github.com/en/rest/actions/workflow-runs), [Docker Compose up](https://docs.docker.com/reference/cli/docker/compose/up/).

## После добавления worker

Начиная с выпуска `0003_operations` updater обновляет и восстанавливает API, web и worker совместно. Worker использует тот же API-образ; различие image IDs блокирует применение. Готовность включает Docker healthcheck worker. Старый baseline без worker требует ручного перехода: [гайд выпуска](vps-worker-rollout.md). Начиная с подготовки `0005_sizing` новый updater поддерживает миграции с временным архивом; переход с уже установленного старого updater требует однократной замены его systemd-скрипта после успешного CI. [Порядок перехода](vps-sizing-rollout.md).

Updater установлен отдельным файлом в `/usr/local/lib/lab-manager/update-vps.py`; обычное обновление образов его не заменяет. Перед выпуском `0006_ledger` с новыми внешними ключами нужно установить версию updater, которая при откате сначала очищает выделенную базу Lab Manager, затем восстанавливает её из временного архива. Архив сохраняется до проверки старой ревизии и готовности сервисов. Одноразовый Compose smoke проверяет этот порядок. Установка нового updater выполняется только из уже прошедшего CI коммита без миграции; затем проверяются `Result=success` и активный timer. Команды конкретного выпуска записываются в его гайд.

### Подтверждённая повторная установка updater

30 сентября пользователь подтвердил успешную установку исправленного updater из коммита `79f52dd2c54749901dd45e98c74d50ed10584538` после [успешного CI](https://github.com/Egoryich/lab-manager/actions/runs/36679735173): `Result=success`, `ExecMainStatus=0`, активный timer и совпадение установленного файла с `git show` этого коммита. Перед установкой `active_sha` уже совпадал с выпуском. Это подтверждает установленный механизм отката, но не миграцию `0006_ledger`.

Для следующего проверенного выпуска **без миграции** общий порядок такой (вместо переменной подставить полный SHA прошедшего CI коммита). Запускать на VPS от root; скрипт установщика не получает stdin из pipe:

```bash
set -euo pipefail
cd /opt/lab-manager/repo
release='<verified-full-sha>'
systemctl start lab-manager-update.service
test "$(python3 -c 'import json; print(json.load(open("/opt/lab-manager/update-state/state.json"))["active_sha"])')" = "$release"
test ! -e /opt/lab-manager/update-state/pending.json
script=$(mktemp /opt/lab-manager/updater-install.XXXXXX.sh)
trap 'rm -f "$script"' EXIT
git show "$release:tools/install-vps-updater.sh" > "$script"
bash "$script" "$release" </dev/null
systemctl start lab-manager-update.service
cmp -s /usr/local/lib/lab-manager/update-vps.py \
  <(git show "$release:tools/update-vps.py")
systemctl show --no-pager lab-manager-update.service -p Result -p ExecMainStatus
systemctl list-timers --all lab-manager-update.timer --no-pager
```

### Подтверждённая автоматическая миграция resource ledger

Пользователь подтвердил установку коммита `8ee4d60f59a9fad60ec0e6bb45921685a2896426` после [успешного CI](https://github.com/Egoryich/lab-manager/actions/runs/36680903243). Сначала timer ожидал публикации образов, затем журнал показал `Verified release: ...; migration=True` и `Migrated and updated application to ...`. В `state.json` активен тот же SHA, PostgreSQL показывает `0006_ledger`, systemd — `Result=success`, `ExecMainStatus=0`. Публичная готовность в этой проверке не вызывалась; локальный `curl` был последней командой блока и её вывод пользователь не прислал. Запуск учебных гостей этим результатом не подтверждён.

Повторяемая проверка миграционного выпуска на VPS, без вывода секретов и адресов:

```bash
set -euo pipefail
cd /opt/lab-manager/repo
systemctl start lab-manager-update.service
python3 -c 'import json; print(json.load(open("/opt/lab-manager/update-state/state.json"))["active_sha"])'
docker compose --env-file .env.vps -f infra/vps/compose.yml \
  exec -T postgres psql -U lab -d lab -Atc \
  'SELECT version_num FROM alembic_version' </dev/null
systemctl show --no-pager lab-manager-update.service -p Result -p ExecMainStatus
journalctl -u lab-manager-update.service -n 12 --no-pager
curl --fail http://127.0.0.1:18000/api/health/ready
```

### Подтверждённое обновление админского экрана хранилищ

30 сентября пользователь запустил `lab-manager-update.service` после выпуска `27b21337414caece8c719291e73e2a32a07fa4e7`. `Result=success`, `ExecMainStatus=0`, журнал несколько раз сообщил `Already current`, а `update-state/state.json` содержит этот SHA. Значит updater уже применил проверенный выпуск с таблицей наблюдаемых хранилищ в Admin UI. В присланном выводе **нет ответа последней команды `curl`**, поэтому отдельная готовность API и отображение страницы в браузере на данном шаге не подтверждены.

Подтверждённые команды проверки (локальный endpoint, без адреса сайта и секретов):

```bash
cd /opt/lab-manager/repo
sudo systemctl start lab-manager-update.service
systemctl show --no-pager lab-manager-update.service -p Result -p ExecMainStatus
sudo journalctl -u lab-manager-update.service -n 15 --no-pager
python3 -c 'import json; print(json.load(open("/opt/lab-manager/update-state/state.json"))["active_sha"])'
```

Следующий шаг — отдельно проверить `curl --fail --silent --show-error --max-time 15 http://127.0.0.1:18000/api/health/ready` и страницу «Серверы» под Admin. Физически свободное место на этой странице не является автоматически доступным для новых машин; admission остаётся закрытым, пока не сверены постоянные дисковые обязательства, чужие машины и сеть.
