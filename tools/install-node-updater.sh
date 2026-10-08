#!/bin/sh
set -eu

if [ "$(id -u)" != 0 ]; then
    echo 'Run as root.' >&2
    exit 1
fi
release=${1:?Pass the full verified release SHA}
printf '%s\n' "$release" | grep -Eq '^[0-9a-f]{40}$' || exit 1
base=/opt/lab-manager-node
stage="$base/releases/$release"
test -f "$stage/SHA256SUMS"
test -f "$stage/update-node.py"
test -f "$stage/lab-node-update.service"
test -f "$stage/lab-node-update.timer"
test -L "$base/current"
systemctl is-active --quiet lab-node-agent.service
systemctl is-active --quiet lab-node-storage-snapshot.timer

cd "$stage"
sha256sum --check --strict SHA256SUMS
for unit in lab-node-agent.service lab-node-storage-snapshot.service lab-node-storage-snapshot.timer; do
    cmp "$unit" "/etc/systemd/system/$unit"
done

if [ ! -x "$stage/venv/bin/python" ]; then
    python3 -m venv "$stage/venv"
    "$stage/venv/bin/python" -m pip --disable-pip-version-check install \
        --no-index --no-deps "$stage"/lab_node_agent-*-py3-none-any.whl
fi
chmod 0755 "$stage"
chmod -R a+rX "$stage/venv"

install -d -o root -g root -m 0755 /usr/local/lib/lab-manager-node
install -o root -g root -m 0644 "$stage/update-node.py" \
    /usr/local/lib/lab-manager-node/update-node.py
install -o root -g root -m 0644 "$stage/lab-node-update.service" \
    /etc/systemd/system/lab-node-update.service
install -o root -g root -m 0644 "$stage/lab-node-update.timer" \
    /etc/systemd/system/lab-node-update.timer
systemctl daemon-reload
if [ ! -e "$base/update-state/state.json" ]; then
    python3 /usr/local/lib/lab-manager-node/update-node.py --adopt
fi
systemctl enable --now lab-node-update.timer
systemctl start --no-block lab-node-update.service
echo 'Node updater installed. Inspect lab-node-update.service and timer.'
