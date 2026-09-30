# Однократное включение автомиграций VPS

Этот гайд фиксирует переход VPS со схемы `0004_nodes` и выпуска `28208ca6c608d07117ff7d6f97838f1f1441610c` на автоматическую доставку миграций. Прежний updater останавливался на `0005_sizing`. Переход не требовал ручного переключения checkout, правки `.env.vps` или вызова Alembic: один раз обновили **сам updater** из успешно проверенного коммита `dev-vps`.

Ниже обобщённая команда установки, сработавшая для этого перехода. Запускать от root на VPS после зелёного CI коммита, содержащего новый updater и образы. Для другого выпуска заранее фиксировать его проверенный SHA, а не автоматически брать непроверенную вершину ветки:

```bash
bash <<'SH'
set -euo pipefail
cd /opt/lab-manager/repo

test ! -e /opt/lab-manager/update-state/pending.json
test -z "$(git status --porcelain --untracked-files=no)"
if systemctl is-active --quiet lab-manager-update.service; then
    echo 'Updater уже работает; дождитесь его завершения' >&2
    exit 1
fi

release='VERIFIED_RELEASE_SHA'
git fetch --no-tags origin dev-vps
git merge-base --is-ancestor "$release" FETCH_HEAD
[[ "$release" =~ ^[0-9a-f]{40}$ ]]
installer=$(mktemp)
trap 'rm -f "$installer"' EXIT
git show "$release:tools/install-vps-updater.sh" > "$installer"
bash "$installer" "$release" </dev/null

systemctl list-timers --all lab-manager-update.timer --no-pager
systemctl show --no-pager lab-manager-update.service -p ActiveState -p Result
echo "Кандидат: $release"
SH
```

Установщик сверяет удалённый Git URL и текущее приложение; скрипт обновления не читает секреты в журнал. Первая проверка запускается без ожидания в shell, поэтому её результат смотрят отдельно. Новая версия updater сама ждёт успешный push CI и публикацию образов именно выбранного SHA. После зелёного CI она скачивает образы, останавливает API/worker, создаёт временную копию БД, применяет `0005_sizing` и запускает новую версию. Временная копия удаляется после подтверждённого успеха. `headscale`, `tailscaled`, Caddy, PostgreSQL и Redis не перезапускаются.

Проверка спустя несколько минут:

```bash
systemctl show --no-pager lab-manager-update.service \
  -p ActiveState -p Result -p ExecMainStatus
journalctl -u lab-manager-update.service -n 30 --no-pager
cd /opt/lab-manager/repo
docker compose --env-file .env.vps -f infra/vps/compose.yml \
  exec -T postgres psql -U lab -d lab -Atc \
  'SELECT version_num FROM alembic_version' </dev/null
docker compose --env-file .env.vps -f infra/vps/compose.yml ps
```

Успех: журнал содержит `Migrated and updated application to ...`, ревизия БД `0005_sizing`, API/web/worker healthy. Если CI ещё выполняется, журнал скажет `Waiting for successful CI...`, и timer повторит проверку. `inactive (dead)` для завершившейся oneshot-службы нормально; ориентир — `Result=success` и `ExecMainStatus=0`.

## Подтверждённый результат 30 сентября 2026

Пользователь выполнил установку из коммита `a3567526e7f935546d54d5c545a5849fec93c150` после [успешного CI](https://github.com/Egoryich/lab-manager/actions/runs/36626154972). Он прислал результат команд проверки выше: `Result=success`, `ExecMainStatus=0`, журнал `Migrated and updated application to a3567526e7f935546d54d5c545a5849fec93c150`, ревизия `0005_sizing`, все пять Compose-сервисов healthy. API и worker работают на одном закреплённом digest. Отдельный внешний запрос после этого вернул HTTP 200 и `{"status":"ready"}`. Старые строки `Release changes schema or Compose` относятся к запускам предыдущего updater до установки нового. Вывод установки пользователь не присылал; подтверждение её результата следует из журнала и состояния сервиса.

Если миграция не удалась **до запуска нового API**, updater возвращает прежние БД и образы, блокирует неудачный SHA и сохраняет причину в журнале. После начала запуска нового API автоматический откат БД запрещён: он мог уже принять новые записи. При сбое на этой стадии updater оставляет `pending.json` и временный архив для разбора; **не удаляйте их и не запускайте повторный ручной deploy**. Новый коммит после исправления создаст новую попытку только при успешно восстановленном исходном состоянии.

Дальнейшие push в `dev-vps` автоматически доставляются после успешного CI. Выпуски, меняющие Compose, пока требуют отдельного согласованного перехода. Сама программа updater обновляется отдельной установкой, когда меняется её механизм: работающий systemd-файл не подменяет свой код во время выполнения.
