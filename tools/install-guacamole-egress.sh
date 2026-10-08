#!/usr/bin/env bash
# Install a narrow, persistent route for one stopped infrastructure VM.
set -euo pipefail
umask 077

if [ "$(id -u)" -ne 0 ] || [ "$#" -ne 6 ]; then
    echo 'Usage (root): install-guacamole-egress.sh VMID BRIDGE UPLINK LAB_POOL RENDERER UNIT' >&2
    exit 2
fi

vmid=$1
bridge=$2
uplink=$3
pool=$4
renderer=$5
unit_source=$6
rules=/etc/lab-manager-node/guacamole-egress.nft
unit=/etc/systemd/system/lab-guacamole-egress.service

[[ "$vmid" =~ ^[1-9][0-9]{2,5}$ ]] && [ "$vmid" -lt 900000 ] || exit 2
[[ "$bridge" =~ ^[a-z][a-z0-9]{1,14}$ ]] || exit 2
[[ "$uplink" =~ ^[a-z][a-z0-9]{1,14}$ ]] || exit 2
test "$bridge" != "$uplink"
test ! -e "$rules" && test ! -e "$unit"
test -f "$renderer" && test -f "$unit_source"
systemctl is-active --quiet lab-node-network-guard.service
test "$(sysctl -n net.ipv4.ip_forward)" = 1
test -d "/sys/class/net/$bridge/bridge"
test -d "/sys/class/net/$uplink/bridge"
test -z "$(find "/sys/class/net/$bridge/brif" -mindepth 1 -maxdepth 1 -print -quit)"
test "$(qm status "$vmid")" = 'status: stopped'

config_file=$(mktemp)
staged=$(mktemp)
trap 'rm -f "$config_file" "$staged"' EXIT
qm config "$vmid" > "$config_file"
parsed=$(python3 - "$bridge" "$config_file" <<'PY'
import ipaddress
from pathlib import Path
import re
import sys

bridge = sys.argv[1]
data = dict(
    line.split(": ", 1)
    for line in Path(sys.argv[2]).read_text().splitlines()
    if ": " in line
)
assert data.get("name") == "lab-guacamole"
assert data.get("onboot", "0") == "0"
net = dict(part.split("=", 1) for part in data["net0"].split(",") if "=" in part)
ipconfig = dict(part.split("=", 1) for part in data["ipconfig0"].split(","))
assert net["bridge"] == bridge and net["firewall"] == "1"
mac = net["virtio"]
assert re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", mac)
interface = ipaddress.ip_interface(ipconfig["ip"])
gateway = ipaddress.ip_address(ipconfig["gw"])
assert isinstance(interface, ipaddress.IPv4Interface)
assert interface.network.prefixlen == 24
assert gateway in interface.network and interface.ip != gateway
print(interface.ip, mac, gateway, sep="\t")
PY
) || { echo 'VM network configuration is not eligible' >&2; exit 1; }
IFS=$'\t' read -r vm_ip vm_mac gateway <<< "$parsed"
ip -o -4 addr show dev "$bridge" | grep -Fq " $gateway/24 "

python3 "$renderer" "$vm_ip" "$vm_mac" "$bridge" "$uplink" "$pool" > "$staged"
nft --check --file "$staged"
systemd-analyze verify "$unit_source"

install -o root -g root -m 0600 "$staged" "$rules"
install -o root -g root -m 0644 "$unit_source" "$unit"
systemctl daemon-reload
systemctl enable --now lab-guacamole-egress.service
systemctl is-active --quiet lab-guacamole-egress.service
nft list table inet lab_guac_filter >/dev/null
nft list table ip lab_guac_nat >/dev/null
nft list table bridge lab_guac_l2 >/dev/null
echo "PASS: Guacamole VM $vmid egress policy active; VM remains stopped"
