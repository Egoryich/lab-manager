# Выпуск очереди и worker на существующий VPS

Команды выполняет пользователь на VPS. Это ручной переход с `0002_catalog` на `0003_operations` и с четырёх на пять контейнеров. Изменены схема и Compose, поэтому прежний updater правильно откажется применять выпуск автоматически. Во время миграции API будет кратковременно недоступен; Caddy, Headscale, Tailscale, PostgreSQL и Redis сохраняются.

Предусловия: успешен весь GitHub workflow конкретного полного SHA ветки `dev-vps`, опубликованы оба образа; текущая установка исправна, нет pending update. Установка приложения уже существует в `/opt/lab-manager/repo`. Эти шаги подготовлены для выпуска; выполнение на пользовательском VPS ещё не подтверждено. Не применять к другой ревизии схемы без разбора.

## 1. Остановить автоматическое обновление и подготовить checkout

Вместо `VERIFIED_COMMIT_SHA` указать полный SHA успешного выпуска. Секреты не выводить.

```bash
cd /opt/lab-manager/repo
export RELEASE_SHA='VERIFIED_COMMIT_SHA'
sudo systemctl disable --now lab-manager-update.timer
systemctl show lab-manager-update.service -p ActiveState -p SubState
```

Продолжать только после завершения уже начатого service (`ActiveState=inactive`); отключение timer не прерывает текущий updater. При failed сначала разобрать journal. Следующий блок проверяет отсутствие незавершённого обновления, локальных правок и свободный диск до изменения checkout:

```bash
sudo env RELEASE_SHA="$RELEASE_SHA" bash <<'SH'
set -euo pipefail
cd /opt/lab-manager/repo
[[ "$RELEASE_SHA" =~ ^[0-9a-f]{40}$ ]]
! systemctl is-active --quiet lab-manager-update.service
test ! -e /opt/lab-manager/update-state/pending.json
test -z "$(git status --porcelain --untracked-files=no)"
python3 -c "import shutil; assert shutil.disk_usage('/opt/lab-manager').free >= 1536*1024**2, 'Need 1.5 GiB free'"
git fetch origin dev-vps
git cat-file -e "$RELEASE_SHA^{commit}"
git merge-base --is-ancestor "$RELEASE_SHA" origin/dev-vps
git checkout --detach "$RELEASE_SHA"
SH
```

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
dc() { docker compose --env-file .env.vps -f infra/vps/compose.yml "$@"; }
revision=$(dc exec -T postgres psql -U lab -d lab -Atc 'SELECT version_num FROM alembic_version')
[[ "$revision" = '0002_catalog' || "$revision" = '0003_operations' ]]
dc stop api worker
dc run --rm --no-deps api /app/.venv/bin/python -m alembic upgrade head
dc up -d --no-deps --wait --wait-timeout 120 api web worker
SH
```

Миграция добавляет таблицы операций, событий и heartbeat; существующие пользователи, группы и профили не удаляются. При ошибке блок прекращается. Не делать `down --volumes`, не удалять БД и не откатывать схему автоматически: сохранить вывод ошибки и разобрать причину. Сохранённая копия env сама по себе не откатывает миграцию.

## 4. Проверить и вернуть CI/CD

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
