# Lab Node Agent

Первый реализованный компонент — read-only Proxmox adapter и CLI `lab-node-agent`. Отдельный Python-пакет без runtime-зависимостей, с собственным lockfile для тестов. Читает inventory по проверенному HTTPS с API token; не изменяет гостей и не открывает сетевой порт.

Целевой агент — Python systemd service на node, типизированные команды, mTLS, durable command receipts, Proxmox/network/storage adapters. Listener, доставка снимков на VPS и автоматическое обновление агента пока не реализованы. CLI и адаптер входят в эту целевую основу; произвольного shell API нет.

[Подготовка установки и границы inventory](../../docs/proxmox-agent-first-install.md).

[План реализации](../../docs/implementation-plan.md).
