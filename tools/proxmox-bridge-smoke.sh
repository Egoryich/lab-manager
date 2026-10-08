#!/usr/bin/env bash
# Reversible L2 bridge-forward check using two private test namespaces.
set -euo pipefail

test "$(id -u)" -eq 0
for device in l2smokebr0 l2testha l2testhb l2testna l2testnb; do
    ! ip link show "$device" >/dev/null 2>&1
done
for namespace in l2testa l2testb; do
    ! ip netns list | awk '{print $1}' | grep -qx "$namespace"
done
test -z "$(ip -4 route show exact 198.18.252.0/29)"
! nft list table bridge l2smoketest >/dev/null 2>&1

cleanup() {
    nft delete table bridge l2smoketest 2>/dev/null || true
    ip link del l2testha 2>/dev/null || true
    ip link del l2testhb 2>/dev/null || true
    ip netns del l2testa 2>/dev/null || true
    ip netns del l2testb 2>/dev/null || true
    ip link del l2smokebr0 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

ip link add l2smokebr0 type bridge
ip link set l2smokebr0 up
ip netns add l2testa
ip netns add l2testb
ip link add l2testha type veth peer name l2testna
ip link add l2testhb type veth peer name l2testnb
ip link set l2testha master l2smokebr0
ip link set l2testhb master l2smokebr0
ip link set l2testha up
ip link set l2testhb up
ip link set l2testna netns l2testa
ip link set l2testnb netns l2testb
ip netns exec l2testa ip addr add 198.18.252.2/29 dev l2testna
ip netns exec l2testb ip addr add 198.18.252.3/29 dev l2testnb
ip netns exec l2testa ip link set l2testna up
ip netns exec l2testb ip link set l2testnb up

ip netns exec l2testa ping -c 3 -W 2 198.18.252.3

nft add table bridge l2smoketest
nft 'add chain bridge l2smoketest guard { type filter hook forward priority -10; policy accept; }'
nft add rule bridge l2smoketest guard meta ibrname l2smokebr0 drop

if ip netns exec l2testa ping -c 2 -W 2 198.18.252.3; then
    echo 'ERROR: nftables did not block bridge-forwarded test traffic' >&2
    exit 1
fi

echo 'OK: nftables blocks L2 traffic between ports of the temporary bridge'
