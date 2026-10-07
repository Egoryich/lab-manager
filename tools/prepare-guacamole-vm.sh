#!/usr/bin/env bash
# Prepare a dedicated Debian cloud VM. Never start or delete a guest automatically.
set -euo pipefail
umask 077

if [ "$(id -u)" -ne 0 ] || [ "$#" -ne 5 ]; then
    echo 'Usage (root): prepare-guacamole-vm.sh VMID STORAGE BRIDGE GUEST_CIDR GATEWAY' >&2
    exit 2
fi

vmid=$1
storage=$2
bridge=$3
guest_cidr=$4
gateway=$5
image='debian-13-generic-amd64.qcow2'
origin='https://cloud.debian.org/images/cloud/trixie/latest'
key=/root/.ssh/lab-manager-guacamole

[[ "$vmid" =~ ^[1-9][0-9]{2,5}$ ]] || { echo 'Invalid VMID' >&2; exit 2; }
if [ "$vmid" -ge 900000 ]; then
    echo 'VMID is reserved for student runtimes' >&2
    exit 2
fi
[[ "$storage" =~ ^[a-z][a-z0-9-]{0,31}$ ]] || { echo 'Invalid storage' >&2; exit 2; }
[[ "$bridge" =~ ^[a-z][a-z0-9]{1,14}$ ]] || { echo 'Invalid bridge' >&2; exit 2; }

python3 - "$guest_cidr" "$gateway" <<'PY'
import ipaddress
import sys

interface = ipaddress.ip_interface(sys.argv[1])
gateway = ipaddress.ip_address(sys.argv[2])
assert isinstance(interface, ipaddress.IPv4Interface)
assert isinstance(gateway, ipaddress.IPv4Address)
assert interface.ip != gateway and gateway in interface.network
assert interface.network.prefixlen == 24
PY

if qm config "$vmid" >/dev/null 2>&1 || pct config "$vmid" >/dev/null 2>&1; then
    echo "VMID $vmid is already allocated" >&2
    exit 1
fi
test -e "/sys/class/net/$bridge"
ip -o -4 addr show dev "$bridge" | grep -Fq " $gateway/24 "
pvesm status | awk -v name="$storage" '
    $1 == name && $3 == "active" && $6 > 20971520 { found = 1 }
    END { exit !found }
'
if [ -e "$key" ] || [ -e "$key.pub" ]; then
    test -s "$key" && test -s "$key.pub"
    test ! -L "$key" && test ! -L "$key.pub"
    test "$(stat -c %u "$key")" = 0
    test "$(stat -c %a "$key")" = 600
fi

stage=$(mktemp -d /var/lib/vz/template/cache/lm-guac-image.XXXXXXXX)
cleanup() {
    rc=$?
    rm -f -- "$stage/$image" "$stage/SHA512SUMS"
    rmdir -- "$stage"
    if [ "$rc" -ne 0 ]; then
        echo "Stopped. Inspect VMID $vmid before retrying; no VM was deleted." >&2
    fi
}
trap cleanup EXIT

for asset in "$image" SHA512SUMS; do
    curl --fail --silent --show-error --location --retry 3 \
        --connect-timeout 15 --max-time 600 \
        --proto '=https' --proto-redir '=https' \
        --output "$stage/$asset" "$origin/$asset"
done
(cd "$stage" && awk -v image="$image" '$2 == image {print; found=1} END {if (!found) exit 1}' \
    SHA512SUMS | sha512sum --check --strict)

install -d -o root -g root -m 0700 /root/.ssh
if [ ! -e "$key" ]; then
    ssh-keygen -q -t ed25519 -N '' -C lab-manager-guacamole -f "$key"
fi
chmod 0600 "$key"
chmod 0644 "$key.pub"
diff -q \
    <(ssh-keygen -y -f "$key" | awk '{print $1, $2}') \
    <(awk '{print $1, $2}' "$key.pub") >/dev/null

qm create "$vmid" \
    --name lab-guacamole --ostype l26 --memory 2048 --cores 2 \
    --scsihw virtio-scsi-single --net0 "virtio,bridge=$bridge,firewall=1" \
    --serial0 socket --vga serial0 --onboot 0
qm disk import "$vmid" "$stage/$image" "$storage"
volume=$(qm config "$vmid" | awk -F ': ' '$1 == "unused0" { split($2, part, ","); print part[1] }')
[[ "$volume" == "$storage":* ]] || { echo 'Imported volume was not found' >&2; exit 1; }
qm set "$vmid" \
    --scsi0 "$volume,discard=on" \
    --ide2 "$storage:cloudinit" \
    --boot order=scsi0 \
    --ipconfig0 "ip=$guest_cidr,gw=$gateway" \
    --nameserver 1.1.1.1 \
    --ciuser labadmin --sshkeys "$key.pub"
qm resize "$vmid" scsi0 16G

echo "Prepared VMID $vmid. It is stopped and onboot is disabled."
echo "SSH key fingerprint: $(ssh-keygen -lf "$key.pub" | awk '{print $2}')"
qm config "$vmid" | grep -E '^(name|memory|cores|net0|scsi0|ide2|ipconfig0|onboot|boot):'
