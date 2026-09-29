# Автообновление VPS без SSH из GitHub

После установки и включения на VPS работает systemd timer: через две минуты после загрузки и затем примерно каждые пять минут запускается проверка. GitHub не подключается к серверу. Updater использует исходящие HTTPS-запросы к публичному репозиторию и GHCR; отдельные токены не нужны, если оба пакета доступны публично.

## Что обновляется автоматически

- Только API и web из текущей вершины `dev-vps`. Применяется один и тот же Git SHA для обоих образов, скачанные образы закрепляются по digest.
- Требуется успешный последний запуск `check.yml` именно для push этого SHA в `dev-vps`, включая jobs `identity-groups` и `vps-images`. Ошибки/незавершённые проверки не позволяют использовать более старый успешный запуск того же SHA.
- Новый SHA должен быть потомком установленного: force-push и откат ветки не приводят к автоматическому downgrade.
- Compose и дерево миграций должны быть неизменны относительно активного выпуска. Изменение схемы, ресурсов/сетей контейнеров, PostgreSQL/Redis или структуры поставки требует отдельного ручного развёртывания. Автоматически Alembic не запускается.
- Updater проверяет отсутствие ручных изменений `.env.vps`, Compose, работающих образов и версии схемы. Перед pull требуется 1.5 GiB свободного места; после pull — 512 MiB. Автоматической очистки образов или томов нет.

Заменяются только API/web с `--no-deps --pull never`; Headscale, Caddy, Tailscale, PostgreSQL и Redis не перезапускаются. При замене возможен краткий перерыв доступа к Lab Manager; это не zero-downtime deployment.

## Ошибки и восстановление

`flock` исключает параллельные запуски updater. Перед заменой сохраняются предыдущие точные image IDs, секреты и запись незавершённой операции. Проверяются локальные API/web и публичный HTTPS readiness. При неудаче восстанавливаются прежние образы без отката схемы. Неудачный SHA блокируется до нового коммита или явного `--retry`.

Если процесс/сервер прерван, следующий запуск обнаруживает pending-запись и сначала возвращает предыдущие образы. Если схема базы или Compose изменились вне updater, автоматический возврат запрещается. Ошибка самого восстановления сохраняет pending-запись для дальнейшего разбора; она не выдаётся за успешный откат.

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

Сервис имеет тип oneshot, поэтому `inactive (dead)` после успешного выполнения нормален; результат смотрите через `systemctl show lab-manager-update.service -p Result -p ExecMainStatus`. Timer должен быть активен.

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

Для ручного выпуска с миграцией/новым Compose отключите timer, дождитесь окончания service, выполните согласованные инструкции конкретного выпуска, переключите checkout на его SHA и задайте оба SHA-тега в `.env.vps`, сохранив секреты. После миграции и проверки выполните `sudo python3 /usr/local/lib/lab-manager/update-vps.py --adopt`, затем снова включите timer. Adopt проверяет совпадение checkout, env, работающих образов и readiness, но не заменяет проверку полного CI и совместимости вручную развёрнутого выпуска. При pending-операции adopt запрещён.

Источники: [GitHub workflow runs API](https://docs.github.com/en/rest/actions/workflow-runs), [Docker Compose up](https://docs.docker.com/reference/cli/docker/compose/up/).

## После добавления worker

Начиная с выпуска `0003_operations` updater обновляет и восстанавливает API, web и worker совместно. Worker использует тот же API-образ; различие image IDs блокирует применение. Готовность включает Docker healthcheck worker. Старый baseline без worker требует ручного перехода: [гайд выпуска](vps-worker-rollout.md). Ранее установленный updater нужно заменить при этом переходе; новые миграции по-прежнему автоматически не применяются.
