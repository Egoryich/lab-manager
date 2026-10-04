#!/usr/bin/env bash
# One disposable Debian LXC on the dedicated student thin pool. No guest network.
set -euo pipefail
umask 077

test "$(id -u)" -eq 0
test "$(/opt/lab-manager-node/current/venv/bin/python -c 'from importlib.metadata import version; print(version("lab-node-agent"))')" = '0.11.0'
systemctl is-active --quiet lab-node-network-guard.service
nft list table inet lab_manager >/dev/null
nft list table bridge lab_manager_l2 >/dev/null

template='local:vztmpl/debian-13-standard_13.6-1_amd64.tar.zst'
storage='student-lvm'
vmid=901001
node=$(hostname -s)
segment=/opt/lab-manager-node/current/venv/bin/lab-node-segment
marker="lab-manager:smoke=$(python3 -c 'import uuid; print(uuid.uuid4())')"
allocation_id=$(python3 -c 'import uuid; print(uuid.uuid4())')
spec=$(mktemp)
bridge=''
completed=0

cleanup() {
    result=$?
    rm -f "$spec"
    if [ "$completed" = 1 ]; then
        echo 'PASS: Debian LXC создан, запущен без сети, остановлен и удалён вместе с диском'
    else
        echo "Проверка прервана. Осмотри VMID=$vmid и сегмент $allocation_id перед очисткой." >&2
    fi
    exit "$result"
}
trap cleanup EXIT

test ! -e "/etc/pve/lxc/$vmid.conf"
test ! -e "/etc/pve/qemu-server/$vmid.conf"
test ! -e "/var/lib/lxc/$vmid"
if lvs --noheadings -o lv_name "$storage" | grep -Eq "^[[:space:]]*vm-$vmid-disk-"; then
    echo "VMID $vmid уже имеет диски; ничего не создаю" >&2
    exit 1
fi
pvesm list local --content vztmpl | grep -Fq "$template"
pvesm status | awk -v name="$storage" '
    $1 == name && $3 == "active" && $6 - 4194304 >= $4 * 0.10 { found=1 }
    END { exit !found }
'

printf '{"allocation_id":"%s","mode":"ISOLATED","cidr":"10.70.255.248/30"}\n' \
    "$allocation_id" > "$spec"
bridge=$($segment create "$spec")
test "${bridge:0:4}" = lmbr

# Proxmox creates /var/lib/lxc/<vmid> under the caller's umask. 077 makes
# it inaccessible to lxc-usernsexec (host UID 100000) during extraction.
umask 022
pct create "$vmid" "$template" \
    --hostname lab-smoke-debian \
    --rootfs "$storage:4" \
    --memory 256 --cores 1 --swap 0 \
    --unprivileged 1 --onboot 0 --start 0 \
    --net0 "name=eth0,bridge=$bridge,firewall=1,ip=manual,ip6=manual,link_down=1" \
    --description "$marker"

verify_owner() {
    pvesh get "/nodes/$node/lxc/$vmid/config" --output-format json |
        python3 -c 'import json,sys; d=json.load(sys.stdin); assert d.get("description") == sys.argv[1]; assert str(d.get("unprivileged")) == "1"; assert "link_down=1" in d.get("net0", ""); print("Владелец и отключённая сеть подтверждены")' "$marker"
}
verify_owner
test "$(pct status "$vmid")" = "status: stopped"

pct start "$vmid"
test "$(pct status "$vmid")" = "status: running"
pct exec "$vmid" -- cat /etc/os-release |
    grep -Eq '^ID=debian$'
if pct exec "$vmid" -- sh -c 'command -v sshd >/dev/null 2>&1'; then
    echo 'SSH-сервер есть в базовом шаблоне'
else
    echo 'SSH-сервера в базовом шаблоне нет; для Guacamole потребуется подготовка образа'
fi

pct shutdown "$vmid" --timeout 60
test "$(pct status "$vmid")" = "status: stopped"
verify_owner
pct destroy "$vmid" --purge
test ! -e "/etc/pve/lxc/$vmid.conf"
if lvs --noheadings -o lv_name "$storage" | grep -Eq "^[[:space:]]*vm-$vmid-disk-"; then
    echo 'После удаления остался том; остановись и проверь вручную' >&2
    exit 1
fi

if [ -e "/sys/class/net/$bridge" ]; then
    test "$(ip -j link show master "$bridge" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')" = 0
    ip link set dev "$bridge" down
fi
test "$($segment delete "$allocation_id")" = DELETED
test ! -e "/sys/class/net/$bridge"
completed=1
