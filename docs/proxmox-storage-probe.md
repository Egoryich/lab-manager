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
