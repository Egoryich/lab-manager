#!/usr/bin/env bash
# Enable the dedicated Guacamole VM's egress policy in one bounded node operation.
set -euo pipefail
umask 077

if [ "$(id -u)" -ne 0 ] || [ "$#" -ne 4 ]; then
    echo 'Usage (root): activate-guacamole-egress.sh VMID BRIDGE UPLINK LAB_POOL' >&2
    exit 2
fi

vmid=$1
bridge=$2
uplink=$3
pool=$4
source_rev=7288af3c64352f66b27d6c117b7dcafa397e099a
tmp=$(mktemp -d)
trap 'rm -f "$tmp/install-guacamole-egress.sh" "$tmp/render-guacamole-egress.py" "$tmp/lab-guacamole-egress.service"; rmdir "$tmp"' EXIT

[[ "$vmid" =~ ^[1-9][0-9]{2,5}$ ]] && [ "$vmid" -lt 900000 ] || exit 2
[[ "$bridge" =~ ^[a-z][a-z0-9]{1,14}$ ]] || exit 2
[[ "$uplink" =~ ^[a-z][a-z0-9]{1,14}$ ]] || exit 2
test "$bridge" != "$uplink"
python3 - "$pool" <<'PY'
import ipaddress
import sys

network = ipaddress.ip_network(sys.argv[1], strict=True)
assert isinstance(network, ipaddress.IPv4Network)
assert network.subnet_of(ipaddress.IPv4Network('10.70.0.0/16'))
PY

test "$(qm status "$vmid")" = 'status: running'
test "$(qm config "$vmid" | sed -n 's/^name: //p')" = 'lab-guacamole'
test ! -e /etc/systemd/system/lab-guacamole-egress.service
test ! -e /etc/lab-manager-node/guacamole-egress.nft
systemctl is-active --quiet lab-node-network-guard.service

for path in \
    tools/install-guacamole-egress.sh \
    tools/render-guacamole-egress.py \
    infra/proxmox/lab-guacamole-egress.service; do
    curl --fail --silent --show-error --location --retry 3 \
        --connect-timeout 15 --max-time 90 \
        --proto '=https' --proto-redir '=https' \
        -o "$tmp/${path##*/}" \
        "https://raw.githubusercontent.com/Egoryich/lab-manager/$source_rev/$path"
done

printf '%s  %s\n' \
    '0df68cd87c53984c3bfd963135d5adb0e80beacf5984d7e1169dd75152064be2' "$tmp/install-guacamole-egress.sh" \
    'bf35d956c362978574ff8bfd7124c173f785ad527440da62c1ac7180d922b415' "$tmp/render-guacamole-egress.py" \
    '8160b825799740800c2defd1ad2579903187c3fd21b4af312eb6c0106f012158' "$tmp/lab-guacamole-egress.service" |
    sha256sum --check --strict

echo 'Stopping only the dedicated Guacamole VM for network policy installation'
qm shutdown "$vmid" --timeout 90
test "$(qm status "$vmid")" = 'status: stopped'

# The installer independently checks the stopped VM's name, MAC, IP,
# dedicated empty bridge, forwarding and nftables syntax. A failure after
# shutdown intentionally leaves the VM stopped for inspection.
bash "$tmp/install-guacamole-egress.sh" \
    "$vmid" "$bridge" "$uplink" "$pool" \
    "$tmp/render-guacamole-egress.py" \
    "$tmp/lab-guacamole-egress.service"

qm start "$vmid"
test "$(qm status "$vmid")" = 'status: running'
systemctl is-active --quiet lab-guacamole-egress.service
nft list table inet lab_guac_filter >/dev/null
nft list table ip lab_guac_nat >/dev/null
nft list table bridge lab_guac_l2 >/dev/null
echo 'PASS: dedicated Guacamole egress policy active; VM running again'
