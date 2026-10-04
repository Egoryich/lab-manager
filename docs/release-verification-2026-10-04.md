# Подтверждённая проверка выпуска 04.10.2026

Эти блоки выполнены на работающем стенде после публикации выпусков. Они не содержат публичных адресов и секретов. Их SHA относятся именно к этой поставке; при следующем обновлении замените ожидаемые SHA на проверенные значения из CI.

## Proxmox, root shell

```bash
bash <<'SH'
set -euo pipefail
expected='5d4b60a60e6873e832c00150d530c88b6b318222'
systemctl start lab-node-update.service
active=$(python3 -c 'import json; print(json.load(open("/opt/lab-manager-node/update-state/state.json"))["active_sha"])')
version=$(/opt/lab-manager-node/current/venv/bin/python -c 'from importlib.metadata import version; print(version("lab-node-agent"))')
test "$active" = "$expected"
test "$version" = '0.14.1'
test ! -e /opt/lab-manager-node/update-state/pending.json
systemctl is-active --quiet lab-node-agent.service lab-node-network-guard.service
echo "PASS Proxmox: $active, агент $version"
SH
```

Результат: `PASS Proxmox`, агент `0.14.1`; служба агента и сетевой guard активны. Блок не открывает допуск гостевых машин.

## VPS, root shell

```bash
bash <<'SH'
set -euo pipefail
cd /opt/lab-manager/repo
expected='908b827701ab787fbe74cab0b925a4fb8b5bd6b0'
systemctl start lab-manager-update.service
test ! -e /opt/lab-manager/update-state/pending.json
active=$(python3 -c 'import json; print(json.load(open("/opt/lab-manager/update-state/state.json"))["active_sha"])')
test "$active" = "$expected"
test -z "$(git status --porcelain --untracked-files=no)"
git fetch origin dev-vps
git merge-base --is-ancestor "$expected" origin/dev-vps
git checkout --detach "$expected"
python3 tools/verify-vps-network-foundation.py
SH
```

Результат: `PASS (9/9 checks)`; схема `0009_commands`, mTLS-инвентарь свежий, принадлежность машин и обязательства по дискам `student-lvm` сверены. На момент проверки оставалось 3.8 GiB на VPS. `admission_ready=false` остаётся ожидаемым: эти проверки подтверждают подготовку, а не готовый запуск занятия.
