#!/usr/bin/env bash
# Reversible packet-path check; never uses a production bridge or guest.
set -euo pipefail

test "$(id -u)" -eq 0
test -z "$(ip -4 route show exact 198.18.254.0/30)"
for device in lmtest0 lmtesth lmtestn; do
    ! ip link show "$device" >/dev/null 2>&1
done
! ip netns list | awk '{print $1}' | grep -qx lmtestns
! nft list table inet lmtest >/dev/null 2>&1

cleanup() {
    nft delete table inet lmtest 2>/dev/null || true
    ip link del lmtesth 2>/dev/null || true
    ip netns del lmtestns 2>/dev/null || true
    ip link del lmtest0 2>/dev/null || true
}
trap cleanup EXIT

ip link add lmtest0 type bridge
ip addr add 198.18.254.1/30 dev lmtest0
ip link set lmtest0 up
ip netns add lmtestns
ip link add lmtesth type veth peer name lmtestn
ip link set lmtesth master lmtest0
ip link set lmtesth up
ip link set lmtestn netns lmtestns
ip netns exec lmtestns ip addr add 198.18.254.2/30 dev lmtestn
ip netns exec lmtestns ip link set lmtestn up

ip netns exec lmtestns ping -c 1 -W 2 198.18.254.1

nft add table inet lmtest
nft 'add chain inet lmtest guard { type filter hook input priority -10; policy accept; }'
nft add rule inet lmtest guard iifname lmtest0 drop

if ip netns exec lmtestns ping -c 1 -W 2 198.18.254.1; then
    echo 'ERROR: nftables did not block the test packet' >&2
    exit 1
fi

echo 'OK: nftables blocks traffic from the temporary lab bridge'
