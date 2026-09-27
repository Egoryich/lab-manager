# Первый запуск VPS-части с существующим Caddy

Это установка текущей реализованной части: аккаунты, группы, профили, права и подготовленные окружения. Worker, резервирование, Proxmox и Guacamole ещё не входят в исполняемую поставку. Команды выполняет пользователь на VPS. Headscale и Tailscale остаются системными сервисами.

## Поставка

После успешных проверок ветки `dev-vps` job `vps-images` собирает два Linux amd64-образа, проверяет Compose с ограничениями памяти и публикует в GHCR. Тег — полный SHA коммита, одинаковый для API и web. Успешный первый job недостаточен: дождитесь успеха всего workflow, включая публикацию обоих образов. Сборка на VPS не требуется.

Первый пакет GHCR может быть приватным. Для публичного исходного кода можно вручную сделать оба пакета `lab-manager-api` и `lab-manager-web` публичными в GitHub Packages → Package settings → Change visibility. Альтернатива — `sudo docker login ghcr.io` с отдельным токеном только `read:packages`, вводимым на VPS; токен не передаётся в чат или Git. После первого проверенного запуска можно установить [pull-updater без SSH из GitHub](vps-auto-update.md).

## Получить конкретную проверенную версию

Подставьте полный SHA **успешного** workflow вместо `VERIFIED_COMMIT_SHA`. Для первого запуска:

```bash
sudo apt install -y git python3
sudo install -d -m 0755 /opt/lab-manager
sudo git clone --branch dev-vps https://github.com/Egoryich/lab-manager.git /opt/lab-manager/repo
cd /opt/lab-manager/repo
sudo git checkout --detach VERIFIED_COMMIT_SHA
sudo python3 tools/init-vps.py --origin https://lab.qround.website --revision VERIFIED_COMMIT_SHA
```

Генератор создаёт `.env.vps` с правами 0600 и случайными секретами. Повторный запуск не перезаписывает файл. Его нужно сохранять при обновлениях: смена encryption/digest keys нарушит доступ к ранее зашифрованным данным и сессиям. Файл исключён из Git. Не присылайте его содержимое или полный вывод `docker compose config`.

## Запустить базу, миграции и приложение

Все дальнейшие команды выполняются из `/opt/lab-manager/repo`. Функция действует только в текущей оболочке:

```bash
dc() { sudo docker compose --env-file .env.vps -f infra/vps/compose.yml "$@"; }
dc config --quiet
dc pull
dc up -d --wait postgres redis
dc run --rm --no-deps api /app/.venv/bin/python -m alembic upgrade head
dc up -d --wait api web
curl --fail http://127.0.0.1:18000/api/health/ready
curl --fail -I http://127.0.0.1:18080/
```

При ошибке остановитесь: не продолжайте публикацию сайта. Не используйте `down -v`, `volume prune`, удаление каталога PostgreSQL или повторную генерацию секретов как способ исправления ошибки. При повторном открытии терминала заново определите `dc`.

## Создать администратора до публикации

```bash
dc exec api /app/.venv/bin/python -m lab_manager.cli bootstrap-admin
```

Введите имя пользователя, отображаемое имя и пароль 12+ символов в интерактивном терминале. Пароль скрывается. Заводских учётных данных нет; существующего администратора команда не меняет.

## Подключить существующий Caddy

Сначала сохраните копию текущего файла, затем откройте его:

```bash
sudo cp -a /etc/caddy/Caddyfile "/etc/caddy/Caddyfile.before-lab.$(date +%Y%m%d%H%M%S)"
sudo nano /etc/caddy/Caddyfile
```

Добавьте **в конец**, сохранив блоки `:80` и `qround.website`:

```caddyfile
lab.qround.website {
    encode zstd gzip
    @api path /api /api/*
    handle @api {
        reverse_proxy 127.0.0.1:18000 {
            header_up X-Forwarded-For {remote_host}
            header_up X-Forwarded-Proto {scheme}
        }
    }
    handle {
        reverse_proxy 127.0.0.1:18080
    }
}
```

```bash
sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile && sudo systemctl reload caddy
curl --fail https://lab.qround.website/api/health/ready
systemctl is-active caddy headscale tailscaled
dc ps
sudo docker stats --no-stream
free -h
df -h /
```

API доверяет forwarded headers только в пределах принятой границы установки: два порта опубликованы на `127.0.0.1`, Caddy перезаписывает IP и протокол, к приватной сети не подключаются сторонние контейнеры. Нельзя менять публикацию API на `0.0.0.0`: это откроет обход Caddy и подмену IP для rate limit. PostgreSQL/Redis вообще не имеют опубликованных портов. Не добавляйте student VM в эти сети. Если перед Caddy появится CDN/ещё один proxy, доверие и извлечение IP нужно настроить отдельно.

## Ресурсы и обновление

Начальные ограничения: API 320 MiB (одна одновременная операция Argon2), PostgreSQL 160 MiB, Redis 48 MiB, web 32 MiB. Сумма 560 MiB — потолки контейнеров, а не доказательство вместимости VPS: Docker, Caddy, Headscale, ОС и kernel требуют дополнительную память. Логи ротируются по 5 MB × 2 на контейнер; Redis не вытесняет rate-limit keys при заполнении и приложение возвращает временный отказ. Swap отсутствует по отчёту пользователя; автоматически его не создаём. Нагрузку реальных пользователей и свободное место проверяем после установки. Guacamole размещается на физическом сервере.

Для ручного обновления нужен новый успешный SHA. Сначала сверяются release notes и совместимость миграций. Обновляются checkout и **только строки образов** в существующем `.env.vps`; секреты сохраняются. Затем pull, согласованное окно миграции и проверка ready. Автоматический downgrade схемы и возврат старого образа после несовместимой миграции не выполняются. Одновременно запускать две установки/миграции нельзя. Если установлен timer, перед ручным обновлением отключите его и следуйте [процедуре adopt](vps-auto-update.md).

Источники: [Docker Ubuntu](https://docs.docker.com/engine/install/ubuntu/), [Caddy reverse_proxy](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy), [GitHub: публикация образов](https://docs.github.com/en/actions/tutorials/publish-packages/publish-docker-images).
