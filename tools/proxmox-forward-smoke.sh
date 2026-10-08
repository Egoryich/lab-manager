#!/usr/bin/env bash
# Reversible routed FORWARD check using two private test namespaces.
set -euo pipefail

test "$(id -u)" -eq 0
test "$(sysctl -n net.ipv4.ip_forward)" = 1
for device in lmfwa lmfwb lmfwa0 lmfwb0 lmfwan lmfwbn; do
    ! ip link show "$device" >/dev/null 2>&1
done
for namespace in lmfwnsa lmfwnsb; do
    ! ip netns list | awk '{print $1}' | grep -qx "$namespace"
done
test -z "$(ip -4 route show exact 198.18.253.0/30)"
test -z "$(ip -4 route show exact 198.18.253.4/30)"
! nft list table inet lmforwardtest >/dev/null 2>&1

cleanup() {
    nft delete table inet lmforwardtest 2>/dev/null || true
    ip link del lmfwa0 2>/dev/null || true
    ip link del lmfwb0 2>/dev/null || true
    ip netns del lmfwnsa 2>/dev/null || true
    ip netns del lmfwnsb 2>/dev/null || true
    ip link del lmfwa 2>/dev/null || true
    ip link del lmfwb 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

ip link add lmfwa type bridge
ip link add lmfwb type bridge
ip addr add 198.18.253.1/30 dev lmfwa
ip addr add 198.18.253.5/30 dev lmfwb
ip link set lmfwa up
ip link set lmfwb up

ip netns add lmfwnsa
ip netns add lmfwnsb
ip link add lmfwa0 type veth peer name lmfwan
ip link add lmfwb0 type veth peer name lmfwbn
ip link set lmfwa0 master lmfwa
ip link set lmfwb0 master lmfwb
ip link set lmfwa0 up
ip link set lmfwb0 up
ip link set lmfwan netns lmfwnsa
ip link set lmfwbn netns lmfwnsb
ip netns exec lmfwnsa ip addr add 198.18.253.2/30 dev lmfwan
ip netns exec lmfwnsb ip addr add 198.18.253.6/30 dev lmfwbn
ip netns exec lmfwnsa ip link set lmfwan up
ip netns exec lmfwnsb ip link set lmfwbn up
ip netns exec lmfwnsa ip route add 198.18.253.4/30 via 198.18.253.1
ip netns exec lmfwnsb ip route add 198.18.253.0/30 via 198.18.253.5

ip netns exec lmfwnsa ping -c 1 -W 2 198.18.253.6

nft add table inet lmforwardtest
nft 'add chain inet lmforwardtest guard { type filter hook forward priority -10; policy accept; }'
nft add rule inet lmforwardtest guard iifname lmfwa oifname lmfwb drop

if ip netns exec lmfwnsa ping -c 1 -W 2 198.18.253.6; then
    echo 'ERROR: nftables did not block routed test traffic' >&2
    exit 1
fi

echo 'OK: nftables blocks routed traffic between temporary lab bridges'
