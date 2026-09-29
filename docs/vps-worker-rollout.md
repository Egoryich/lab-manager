# Выпуск очереди и worker на существующий VPS

Команды выполняет пользователь на VPS. Это ручной переход с `0002_catalog` на `0003_operations` и с четырёх на пять контейнеров. Изменены схема и Compose, поэтому прежний updater правильно откажется применять выпуск автоматически. Во время миграции API будет кратковременно недоступен; Caddy, Headscale, Tailscale, PostgreSQL и Redis сохраняются.

Предусловия: успешен весь GitHub workflow конкретного полного SHA ветки `dev-vps`, опубликованы оба образа; текущая установка исправна, нет pending update. Установка приложения уже существует в `/opt/lab-manager/repo`. Подготовка, скачивание, миграция и запуск worker подтверждены пользователем 29.09.2026; принятие нового baseline и повторное включение timer также подтверждены. Проверка браузерного сценария на VPS ещё ожидается. Не применять к другой ревизии схемы без разбора.

## Подтверждённая подготовка — 29 сентября

После добавления RAM и reboot пользователь выполнил на VPS:

```bash
cd /opt/lab-manager/repo
sudo systemctl disable --now lab-manager-update.timer
systemctl show --no-pager lab-manager-update.service -p ActiveState -p SubState
sudo journalctl -u lab-manager-update.service -n 15 --no-pager
sudo test ! -e /opt/lab-manager/update-state/pending.json \
  && echo "Незавершённых обновлений нет"
free -h
df -h /
```

Результат по присланному выводу: timer отключён, service `inactive/dead`, pending отсутствует; RAM 1.9 GiB всего / 1.4 GiB available, диск 4.2 GiB свободно, swap 0. Журнал подтвердил прежний активный выпуск `4e0567515c6a4dc2cfd9553932f8a352ecc39343`. Отказ `Release changes schema or Compose; manual deployment required` — ожидаемая защита от автоматической миграции, а не подтверждение поломки приложения. Готовность приложения после reboot проверяется отдельно.

Целевой выпуск очереди `fbdbc5a3a03a0ae0abb49d8fea002c9708150f8b`: весь [GitHub workflow](https://github.com/Egoryich/lab-manager/actions/runs/36524343999) завершился успешно, включая публикацию обоих образов. Это подтверждение CI, не выполнения команд на VPS.

## 1. Остановить автоматическое обновление и подготовить checkout

Вместо `VERIFIED_COMMIT_SHA` указать полный SHA успешного выпуска. Секреты не выводить.

```bash
cd /opt/lab-manager/repo
export RELEASE_SHA='VERIFIED_COMMIT_SHA'
sudo systemctl disable --now lab-manager-update.timer
systemctl show --no-pager lab-manager-update.service -p ActiveState -p SubState
```

Продолжать только после завершения уже начатого service (`ActiveState=inactive`); отключение timer не прерывает текущий updater. При failed сначала разобрать journal. Следующий блок проверяет отсутствие незавершённого обновления, локальных правок и свободный диск до изменения checkout:

```bash
sudo env RELEASE_SHA="$RELEASE_SHA" bash <<'SH'
set -euo pipefail
cd /opt/lab-manager/repo
[[ "$RELEASE_SHA" =~ ^[0-9a-f]{40}$ ]]
if systemctl is-active --quiet lab-manager-update.service; then
    echo 'Wait for the running updater to finish' >&2
    exit 1
fi
test ! -e /opt/lab-manager/update-state/pending.json
test -z "$(git status --porcelain --untracked-files=no)"
python3 -c "import shutil; assert shutil.disk_usage('/opt/lab-manager').free >= 1536*1024**2, 'Need 1.5 GiB free'"
git fetch origin dev-vps
git cat-file -e "$RELEASE_SHA^{commit}"
git merge-base --is-ancestor "$RELEASE_SHA" origin/dev-vps
git checkout --detach "$RELEASE_SHA"
SH
```

Перед изменением `.env.vps` можно отдельно скачать образы проверенного выпуска и проверить готовность существующего приложения. Команды подтверждены присланным пользователем выводом 29.09.2026 после успешных fetch, проверки ancestry и checkout:

```bash
sudo docker pull "ghcr.io/egoryich/lab-manager-api:${RELEASE_SHA:?Set verified release SHA}"
sudo docker pull "ghcr.io/egoryich/lab-manager-web:${RELEASE_SHA:?Set verified release SHA}"
curl --fail http://127.0.0.1:18000/api/health/ready
df -h /
```

Checkout теперь `fbdbc5a3a03a0ae0abb49d8fea002c9708150f8b`. Оба pull успешны: API digest `sha256:109907e183e4e42914c0602f9fcf9d185fa0d666f175595e52f59953f0245df9`, web digest `sha256:c6a56040ed4cd3e675da9a0396b9d306af51e543a246c52d3bfc1e8d375e439e`. Существующий API вернул `ready`; свободно 4.0 GiB (55% занято). Checkout ранее оставался на `9312393`, что нормально: updater обновлял контейнеры без переключения checkout. Скачивание образов ещё не подтверждает смену работающих контейнеров или миграцию.

## 2. Сохранить настройки и подготовить новые образы

Этот блок меняет только ссылки на два образа; пароли, ключи и домен сохраняются. Исходная копия `.env.vps` остаётся root-only в `update-state/before-worker.env`. Не присылать её в чат. Если блок прервётся, дальнейшие шаги не выполнять до разбора; существующие контейнеры пока продолжают работать.

```bash
sudo env RELEASE_SHA="$RELEASE_SHA" bash <<'SH'
set -euo pipefail
cd /opt/lab-manager/repo
test "$(git rev-parse HEAD)" = "$RELEASE_SHA"
python3 - "$RELEASE_SHA" <<'PY'
import importlib.util, pathlib, sys
spec = importlib.util.spec_from_file_location('updater', 'tools/update-vps.py')
u = importlib.util.module_from_spec(spec)
spec.loader.exec_module(u)
sha = sys.argv[1]
assert u.SHA.fullmatch(sha)
run = u.verified_run(u.github_json(
    f'actions/workflows/check.yml/runs?branch=dev-vps&event=push&head_sha={sha}&per_page=20'
), sha)
assert run, 'Full release CI must succeed first'
jobs = u.github_json(f"actions/runs/{run['id']}/jobs?per_page=100")
assert {'identity-groups', 'vps-images'} <= {
    j['name'] for j in jobs['jobs'] if j['status']=='completed' and j['conclusion']=='success'
}
env = pathlib.Path('.env.vps')
previous = pathlib.Path('/opt/lab-manager/update-state/before-worker.env')
if not previous.exists():
    u.atomic(previous, env.read_text())
u.atomic(env, u.image_env(env.read_text(),
    f'ghcr.io/egoryich/lab-manager-api:{sha}', f'ghcr.io/egoryich/lab-manager-web:{sha}'))
PY
docker compose --env-file .env.vps -f infra/vps/compose.yml pull api web
python3 -c "import shutil; assert shutil.disk_usage('/opt/lab-manager').free >= 512*1024**2, 'Need 512 MiB free after pull'"
SH
```

## 3. Применить миграцию и запустить приложение

Не запускать из двух терминалов одновременно. Проверить текущую схему: ожидается `0002_catalog`; `0003_operations` допустима при повторе уже выполненной миграции этого выпуска. При другом результате остановиться.

```bash
dc() { sudo docker compose --env-file /opt/lab-manager/repo/.env.vps -f /opt/lab-manager/repo/infra/vps/compose.yml "$@"; }
dc exec -T postgres psql -U lab -d lab -Atc 'SELECT version_num FROM alembic_version'
```

```bash
sudo bash <<'SH'
set -euo pipefail
cd /opt/lab-manager/repo
dc() { docker compose --env-file .env.vps -f infra/vps/compose.yml "$@" </dev/null; }
revision=$(dc exec -T postgres psql -U lab -d lab -Atc 'SELECT version_num FROM alembic_version')
[[ "$revision" = '0002_catalog' || "$revision" = '0003_operations' ]]
dc stop api worker
dc run --rm --no-deps api /app/.venv/bin/python -m alembic upgrade head
dc up -d --no-deps --wait --wait-timeout 120 api web worker
SH
```

Миграция добавляет таблицы операций, событий и heartbeat; существующие пользователи, группы и профили не удаляются. При ошибке блок прекращается. Не делать `down --volumes`, не удалять БД и не откатывать схему автоматически: сохранить вывод ошибки и разобрать причину. Сохранённая копия env сама по себе не откатывает миграцию.

## 4. Проверить и вернуть CI/CD

Подтверждено пользователем 29.09.2026: исходная схема `0002_catalog`, сохранение прежнего env через `atomic` и замена только ссылок на образы, затем `alembic upgrade head` и запуск API/web/worker завершились успешно. Исполненный блок использовал `flock -n 9` на `update-state/update.lock`, проверки HEAD/pending/timer/service, перенаправление stdin Docker в `/dev/null`, `--pull never` у `run` и `up`. Эти ограничения сохраняются при повторении из shell here-document. Updater пока отключён.

Фактически выполненные команды запуска после подготовки env:

```bash
dc() { docker compose --env-file .env.vps -f infra/vps/compose.yml "$@" </dev/null; }
dc stop api worker
dc run --rm --no-deps --pull never api /app/.venv/bin/python -m alembic upgrade head
dc up -d --no-deps --pull never --wait --wait-timeout 120 api web worker
curl --fail http://127.0.0.1:18000/api/health/ready
dc ps
docker stats --no-stream
```

Место выполнения — root-shell на VPS из `/opt/lab-manager/repo`, после остановки timer и захвата lock. Результат: пять healthy контейнеров, локальный API вернул `ready`; API и worker используют выпуск `fbdbc5a3a03a0ae0abb49d8fea002c9708150f8b`. Readiness этого выпуска проверяет `0003_operations`, что вместе с успешной миграцией подтверждает новую схему. Снимок RAM контейнеров: worker 68.14 MiB / 128 MiB, API 74.39 / 320, web 2.191 / 32, PostgreSQL 83.64 / 160, Redis 37.9 / 48. Это ранний снимок после запуска, не нагрузочный тест. Публичный HTTPS и пользовательский сценарий новой проверки ещё требуют подтверждения.

```bash
curl --fail http://127.0.0.1:18000/api/health/ready
dc ps
sudo docker stats --no-stream
free -h
df -h /
```

Ожидается `ready` и пять healthy контейнеров, включая worker. В интерфейсе преподавателя: открыть подготовленное окружение → «Проверить конфигурацию» → «Проверка завершена». Бронь и машины этим действием не создаются.

После этого новый updater принимает вручную проверенный выпуск, проверяет также публичный HTTPS и начинает обновлять API/worker совместно:

```bash
cd /opt/lab-manager/repo
sudo python3 tools/update-vps.py --adopt
# Только если adopt завершился успешно:
sudo bash tools/install-vps-updater.sh "$RELEASE_SHA"
systemctl list-timers --all lab-manager-update.timer --no-pager
sudo journalctl -u lab-manager-update.service -n 30 --no-pager
```

Для будущих выпусков без изменения схемы/Compose — прежний автоматический процесс. Worker использует тот же образ, что API; drift между ними блокирует обновление. Новые миграции по-прежнему требуют отдельного выпуска. Лимиты контейнеров теперь суммарно 688 MiB; фактический расход VPS и нагрузочная вместимость проверяются после установки, лимиты не означают заранее занятую память.

## Подтверждение повторного включения updater — 29 сентября

Пользователь выполнил `python3 tools/update-vps.py --adopt </dev/null`, затем `bash tools/install-vps-updater.sh "$RELEASE_SHA" </dev/null` из root-shell VPS. Вывод: `Adopted manually verified running release`, `Already current: fbdbc5a3a03a0ae0abb49d8fea002c9708150f8b`, `Updater installed`; timer имеет следующий запуск. Adopt включает проверку публичного HTTPS и готовности worker. Это подтверждает принятие установленного выпуска и включение timer, но не доставку следующего нового выпуска.

После таблицы timer пользователь сообщил об остановившемся выводе. Следующая команда была `systemctl show` без `--no-pager`; вероятная причина — интерактивный pager. Выход через `q`; если не сработает, прервать только оставшуюся диагностику через Ctrl+C. В гайде команды `show` теперь явно отключают pager. Причина ожидания остаётся предположением до ответа пользователя. Повторять установку для проверки статуса не нужно:

```bash
systemctl show --no-pager lab-manager-update.service -p Result -p ExecMainStatus
systemctl is-active lab-manager-update.timer
sudo journalctl -u lab-manager-update.service -n 15 --no-pager
```

### Проверка без pager завершена успешно

29.09.2026 пользователь повторил принятие baseline и установку updater с явным `--no-pager` у команды `systemctl show`. Весь блок завершился: `Result=success`, `ExecMainStatus=0`; timer назначил следующий запуск. Journal показал несколько успешных периодических проверок `Already current: fbdbc5a3a03a0ae0abb49d8fea002c9708150f8b`. Повторный запуск установки сохранил исправное состояние. Исходная причина ожидания не доказана отдельно; вариант без pager подтверждён рабочим.

Успешный порядок команд (VPS, root-shell, проверенный checkout, без удержания внешнего update.lock):

```bash
set -euo pipefail
cd /opt/lab-manager/repo
# RELEASE_SHA — полный SHA уже установленного и проверенного выпуска.
test "$(git rev-parse HEAD)" = "${RELEASE_SHA:?Set verified installed release SHA}"
python3 tools/update-vps.py --adopt </dev/null
bash tools/install-vps-updater.sh "$RELEASE_SHA" </dev/null
systemctl list-timers --all lab-manager-update.timer --no-pager
systemctl show --no-pager lab-manager-update.service -p Result -p ExecMainStatus
journalctl -u lab-manager-update.service -n 15 --no-pager
```

VPS-переход завершён: схема, пять контейнеров, baseline и периодические проверки updater подтверждены пользователем. Проверка конфигурации через браузер на VPS и доставка следующего нового SHA остаются отдельными, ещё не подтверждёнными сценариями.

### Браузерная проверка на VPS подтверждена

29.09.2026 пользователь выполнил проверку конфигурации окружения через сайт и сообщил: «Проверка завершена — успех». Подтверждена цепочка UI → API → PostgreSQL queue → worker → результат в интерфейсе на целевой установке. Это конфигурационная проверка, не резервирование ресурсов и не запуск Proxmox-машин. Следующее новое автоматическое обновление SHA после включения worker ещё не подтверждено.
