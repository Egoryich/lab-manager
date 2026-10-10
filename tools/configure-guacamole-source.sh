#!/usr/bin/env bash
# Bind the root-owned SSH admission source to the already protected Guacamole VM.
set -euo pipefail
umask 077

if [ "$(id -u)" -ne 0 ] || [ "$#" -ne 2 ]; then
    echo 'Usage (root): configure-guacamole-source.sh VMID BRIDGE' >&2
    exit 2
fi
vmid=$1
bridge=$2
target=/etc/lab-manager-node/guacamole-source.json
[[ "$vmid" =~ ^[1-9][0-9]{2,5}$ ]] && [ "$vmid" -lt 900000 ] || exit 2
[[ "$bridge" =~ ^[a-z][a-z0-9]{1,14}$ ]] || exit 2
test ! -e "$target" && test ! -L "$target"
test "$(qm status "$vmid")" = 'status: running'
systemctl is-active --quiet lab-node-network-guard.service lab-guacamole-egress.service
nft list table inet lab_guac_filter >/dev/null

config=$(mktemp)
candidate=$(mktemp /etc/lab-manager-node/guacamole-source.XXXXXX)
trap 'rm -f "$config" "$candidate"' EXIT
qm config "$vmid" > "$config"
python3 - "$config" "$bridge" "$candidate" <<'PY'
import ipaddress
import json
from pathlib import Path
import sys

config, bridge, candidate = sys.argv[1:]
values = dict(
    line.split(': ', 1)
    for line in Path(config).read_text().splitlines()
    if ': ' in line
)
assert values.get('name') == 'lab-guacamole'
network = dict(part.split('=', 1) for part in values['net0'].split(',') if '=' in part)
address = dict(part.split('=', 1) for part in values['ipconfig0'].split(','))
assert network.get('bridge') == bridge and network.get('firewall') == '1'
interface = ipaddress.ip_interface(address['ip'])
gateway = ipaddress.ip_address(address['gw'])
assert isinstance(interface, ipaddress.IPv4Interface)
assert interface.network.prefixlen == 24 and gateway in interface.network
assert interface.ip != gateway
Path(candidate).write_text(json.dumps({'address': str(interface.ip), 'bridge': bridge}) + '\n')
PY
chown root:root "$candidate"
chmod 0600 "$candidate"
mv -T "$candidate" "$target"
echo 'PASS: protected Guacamole source configured for SSH admission'
