#!/usr/bin/env bash
# One-time, fail-closed installation of the deny-only guard on a Proxmox node.
set -euo pipefail

test "$(id -u)" -eq 0
test "$#" -eq 2
rules=$1
unit=$2
test -s "$rules"
test -s "$unit"
test -x /usr/sbin/nft
test -d /etc/lab-manager-node
test ! -e /etc/lab-manager-node/network-guard.nft
test ! -L /etc/lab-manager-node/network-guard.nft
test ! -e /etc/systemd/system/lab-node-network-guard.service
test ! -L /etc/systemd/system/lab-node-network-guard.service
if systemctl is-enabled --quiet nftables.service ||
   systemctl is-active --quiet nftables.service; then
    echo 'nftables.service is enabled or active: inspect its ruleset ordering first' >&2
    exit 1
fi

links=$(ip -o link show)
if grep -Eq '^[[:digit:]]+: lmbr[^:]*:' <<< "$links"; then
    echo 'Existing lmbr interface: inspect it before installing the guard' >&2
    exit 1
fi
if /usr/sbin/nft list table inet lab_manager >/dev/null 2>&1 ||
   /usr/sbin/nft list table bridge lab_manager_l2 >/dev/null 2>&1; then
    echo 'Lab Manager nftables table already exists: refusing replacement' >&2
    exit 1
fi

/usr/sbin/nft --check --file "$rules"
systemd-analyze verify "$unit"
install -o root -g root -m 0644 "$rules" /etc/lab-manager-node/network-guard.nft
install -o root -g root -m 0644 "$unit" /etc/systemd/system/lab-node-network-guard.service
systemctl daemon-reload
systemctl enable --now lab-node-network-guard.service
/usr/sbin/nft list table inet lab_manager >/dev/null
/usr/sbin/nft list table bridge lab_manager_l2 >/dev/null
systemctl is-active --quiet lab-node-network-guard.service
echo 'Deny-only guard installed; no guest bridge was attached'
