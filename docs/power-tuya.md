# Включение через Tuya

Принято DEC-01: PCIe-плата, связанная с PWR/RESET, управляется через Tuya Cloud. Пользователь предоставил functions switch_1 и ModeReset, Central Europe endpoint и пример протокола. API credentials не запрашивались; здесь нет команды к реальному устройству.

## Подтверждённый пользователем протокол

API URL: https://openapi.tuyaeu.com. POST /v1.0/iot-03/devices/{device_id}/commands; GET /v1.0/iot-03/devices/{device_id}/status. Поля подключения: TUYA_ACCESS_ID, TUYA_ACCESS_SECRET, TUYA_DEVICE_ID, TUYA_API_URL; секреты только на VPS.

| Действие | Payload команды | Применение |
|---|---|---|
| Power | switch_1 = true | Единственный штатный Tuya action при запросе включения после проверки состояния |
| Power off | switch_1 = false | Поведение зависит от firmware; не основной способ завершения Proxmox |
| Reset | ModeReset = Reset | Отдельная административная операция, физический reset нельзя считать graceful OS reboot |
| Force Reset | ModeReset = forceReset | Только отдельное emergency действие Admin после явного подтверждения |
| Неопределённый enum | ModeReset = 0 | Не используется |

Поля и смысл commands взяты из описания конкретной платы пользователем, а не из универсальной Tuya категории. Семантику физической кнопки/повтора нужно один раз проверить. Официальный маршрут команд подтверждён в [Tuya Send commands](https://developer.tuya.com/en/docs/cloud/e2512fb901?id=Kag2yag3tiqn5), региональные endpoints — в [Request Structure](https://developer.tuya.com/en/docs/iot/api-request?id=Ka4a8uuo1j4t4).

Backend остаётся Python/FastAPI. Официальный [tuya-connector-python](https://github.com/tuya/tuya-connector-python) — кандидат для transport adapter; совместимость с Python 3.13+ и async worker проверяется перед фиксацией lockfile. Синхронный SDK не блокирует event loop. Пример Express/Node пользователя — справка по API, не требование заменить стек или сделать неавторизованный power endpoint.

## Два разных вида устройств

| Подключение | Что означает команда | Что нельзя предполагать |
|---|---|---|
| Контакты Power на материнской плате | Краткий импульс кнопки | Повтор импульса может завершить работу уже включённой машины; state реле не равен state компьютера |
| Коммутация питания | Подача/снятие питания | Подача питания сама по себе не доказывает boot; отключение питания не graceful shutdown |

Для выбранной PCIe-платы зафиксирован PWR/RESET путь. Дежурное питание платы и доступность Wi-Fi/роутера проверяются при выключенном ПК. Lab Node Agent на выключенном Proxmox не может обеспечить этот канал.

## Provider

`TuyaPowerProvider` реализует `power_on`, `power_status` и capabilities для `power_off`. Обычное выключение физического node выполняет Proxmox/Agent после DRAINING; Tuya не заменяет корректное завершение ОС. Для платы-кнопки применяется ровно подтверждённый импульс, а не условный `toggle`.

Для cloud-варианта официальный Tuya API позволяет узнать functions/specifications устройства, отправить commands и получить reported status. Конкретный datapoint не называется заранее `switch_1`: он берётся из модели устройства. Права проекта, регион аккаунта и доступность API проверяются до выбора transport. [Tuya Device Control](https://developer.tuya.com/en/docs/cloud/device-control?id=K95zu01ksols7).

Cloud credential хранится на VPS в SecretStore. Отдельный local relay для выбранного Cloud transport не требуется. Реальные API status fields пока не представлены: pc status = ON/OFF в логах нужно сопоставить с ответом API, а switch_1 нельзя без проверки принять за телеметрию питания ПК.

## Надёжность

- Записать Operation intent до отправки. Одновременные нажатия Teacher сходятся к одному power workflow.
- Timeout API означает UNKNOWN, не «импульс не был доставлен». Повтор не разрешён до сверки или явной процедуры восстановления.
- `power_status` сообщает отдельно relay state, доказательства состояния компьютера и время наблюдения. По включённому Wi-Fi реле нельзя объявлять node READY.
- Туннель, Agent, Proxmox, storage, network, gateway и fresh inventory должны подтвердить boot. Пока этого нет, UI показывает этап и timeout.
- Обычная кнопка выключения не снимает питание с работающих учебных машин. Emergency действие доступно Admin отдельно и не выдаётся за сохранение состояния.

## Проверка на стенде

Холодное включение из выключенного состояния; повторный пользовательский запрос; потеря ответа Tuya; команда при уже включённом сервере; отсутствие Интернета/питания платы; задержка boot; выключение после сохранения гостей; восстановление после смены внешнего IP. Реальный datapoint и допустимая длительность импульса фиксируются из документации конкретной платы и испытания.
