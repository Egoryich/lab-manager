#!/bin/sh
set -eu

if [ "$(id -u)" != 0 ]; then
    echo 'Run this installer with sudo.' >&2
    exit 1
fi
revision=${1:?Pass the full verified Git commit SHA}
printf '%s\n' "$revision" | grep -Eq '^[0-9a-f]{40}$' || exit 1
repo=/opt/lab-manager/repo
test -f "$repo/.env.vps"
test "$(git -C "$repo" remote get-url origin)" = 'https://github.com/Egoryich/lab-manager.git'
if systemctl is-active --quiet lab-manager-update.service; then
    echo 'An update is running. Wait for it to finish before installing the updater.' >&2
    exit 1
fi
# Stop only the previous polling timer, if installed; application services stay up.
if systemctl cat lab-manager-update.timer >/dev/null 2>&1; then
    systemctl stop lab-manager-update.timer
fi
stage=$(mktemp -d /opt/lab-manager/updater-install.XXXXXX)
trap 'rm -f "$stage/update-vps.py" "$stage/lab-manager-update.service" "$stage/lab-manager-update.timer"; rmdir "$stage"' EXIT
git -C "$repo" show "$revision:tools/update-vps.py" > "$stage/update-vps.py"
git -C "$repo" show "$revision:infra/vps/lab-manager-update.service" > "$stage/lab-manager-update.service"
git -C "$repo" show "$revision:infra/vps/lab-manager-update.timer" > "$stage/lab-manager-update.timer"
install -d -o root -g root -m 0755 /usr/local/lib/lab-manager
install -o root -g root -m 0644 "$stage/update-vps.py" /usr/local/lib/lab-manager/update-vps.py
# Bootstrap verifies that the checkout, env and healthy running containers agree.
# Do not checkout the new release before this step on the first installation.
python3 /usr/local/lib/lab-manager/update-vps.py --check
install -o root -g root -m 0644 "$stage/lab-manager-update.service" /etc/systemd/system/lab-manager-update.service
install -o root -g root -m 0644 "$stage/lab-manager-update.timer" /etc/systemd/system/lab-manager-update.timer
systemctl daemon-reload
systemctl enable --now lab-manager-update.timer
systemctl start lab-manager-update.service
echo 'Updater installed. Inspect: journalctl -u lab-manager-update.service -n 30 --no-pager'
