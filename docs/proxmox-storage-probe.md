# Локальная диагностика LVM-thin на Proxmox

`lab_node_agent.host_storage` — диагностический модуль wheel 0.3.0. Администратор запускает его **локально от root**; установленная непривилегированная служба агента не вызывает `lvs` и не получает дополнительные права. Модуль читает только секции `lvmthin` из `/etc/pve/storage.cfg` и фиксированной командой `lvs` получает размеры пулов, заполнение data/metadata и список томов. Он выводит JSON без токена, содержимого конфигураций машин и других секций storage.cfg. Ошибки выводятся фиксированным кодом.

API содержимого Proxmox недостаточно для этого этапа: его read-only endpoint доступен с `Datastore.Audit`, но по [исходному коду Proxmox](https://github.com/proxmox/pve-storage/blob/master/src/PVE/API2/Storage/Content.pm) каждый том дополнительно проходит проверку доступа и может быть пропущен. Текущий read-only token не получает права изменения дисков ради полноты списка. Формат `lvs --reportformat json` и поля LVM описаны в [руководстве lvs](https://man7.org/linux/man-pages/man8/lvs.8.html).

На подготовленном Proxmox после проверки SHA выпуска и wheel:

```bash
# stage указывает на каталог с проверенным wheel новой версии.
python3 -m venv "$stage/venv" </dev/null
"$stage/venv/bin/python" -m pip --disable-pip-version-check install \
  --no-index --no-deps "$stage/lab_node_agent-0.3.0-py3-none-any.whl" </dev/null
"$stage/venv/bin/python" -m lab_node_agent.host_storage
```

Поле `owner_vmid_from_name` — лишь число из имени LV, а не подтверждение владельца. `ownership_reconciled=false` и `admission_ready=false` обязательны даже при успешном выводе. Данные ещё не связываются с бронями и гостевыми конфигурациями; для полного допуска также нужны проверка снимков, физической основы storage, управление созданными Lab Manager томами и синхронизация наблюдения с операциями. Чужие и тестовые диски нельзя считать свободными и нельзя удалять автоматически.

После реальной проверки команды и безопасные результаты дополняются здесь без адресов, секретов и серийных номеров устройств.

## Первая проверка на физическом сервере

Пользователь запустил wheel выпуска `bbd5e3c7120ac0fe68db7495432706b957560297` отдельно от службы. Он подтвердил внешний SHA256 файла `SHA256SUMS`, затем проверку всех четырёх release assets, установку wheel 0.3.0 в отдельный venv и `active` у прежней службы. Сводка локального read-only probe показала:

Обобщённая последовательность успешной подготовки (адрес релиза, SHA и ожидаемый хеш manifest брать из проверенного CI выпуска; секреты и адреса серверов не нужны):

```bash
set -euo pipefail
umask 077
release='<verified-full-sha>'
release_url='<trusted-release-url>'
manifest_sha256='<verified-manifest-sha256>'
stage="/opt/lab-manager-node/releases/$release"
test ! -e "$stage"
install -d -m 0700 "$stage"
cd "$stage"
for asset in lab_node_agent-0.3.0-py3-none-any.whl \
             lab-node-agent.service node-pki.py SHA256SUMS; do
  curl --fail --location --retry 3 --connect-timeout 15 --max-time 180 \
    --proto '=https' --proto-redir '=https' -o "$asset" \
    "$release_url/$asset"
done
printf '%s  %s\n' "$manifest_sha256" SHA256SUMS | sha256sum --check --strict
sha256sum --check --strict SHA256SUMS
python3 -m venv "$stage/venv" </dev/null
"$stage/venv/bin/python" -m pip --disable-pip-version-check install \
  --no-index --no-deps "$stage/lab_node_agent-0.3.0-py3-none-any.whl" </dev/null
"$stage/venv/bin/python" -m lab_node_agent.host_storage
systemctl is-active lab-node-agent.service
```

| Storage | VG / thin pool | Pool bytes | Data | Metadata | Найдено томов |
| --- | --- | ---: | ---: | ---: | ---: |
| `local-lvm` | `pve` / `data` | 67 641 540 608 | 0.0% | 1.6% | 0 |
| `student-lvm` | `student-lvm` / `student-lvm` | 489 970 204 672 | 0.0% | 0.38% | 0 |

В прежнем диагностическом выводе `lvs` были тестовые `vm-100-disk-0` и `vm-201-disk-0`; данный снимок их не показывает. Пока нельзя заключить, что тома удалены: нужно отдельно сравнить полный список LVM и конфигурации гостей. Поэтому нулевой счётчик **не означает**, что дисковые обязательства равны нулю. `admission_ready=false` сохраняется.
