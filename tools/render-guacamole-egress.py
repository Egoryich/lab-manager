"""Render a Proxmox-owned, fail-closed egress policy for the infrastructure VM."""

import argparse
import ipaddress
import re

BRIDGE = re.compile(r"[a-z][a-z0-9]{1,14}\Z")
MAC = re.compile(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\Z")
PRIVATE_DESTINATIONS = (
    "10.0.0.0/8",
    "100.64.0.0/10",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "224.0.0.0/4",
    "240.0.0.0/4",
)


def render(vm_ipv4: str, vm_mac: str, bridge: str, uplink: str, lab_pool: str) -> str:
    address = ipaddress.ip_address(vm_ipv4)
    pool = ipaddress.ip_network(lab_pool, strict=True)
    if (
        not isinstance(address, ipaddress.IPv4Address)
        or not address.is_private
        or not isinstance(pool, ipaddress.IPv4Network)
        or not pool.subnet_of(ipaddress.ip_network("10.70.0.0/16"))
        or not BRIDGE.fullmatch(bridge)
        or not BRIDGE.fullmatch(uplink)
        or not MAC.fullmatch(vm_mac)
        or bridge == uplink
    ):
        raise ValueError("INVALID_GUACAMOLE_EGRESS_CONFIG")
    private = ", ".join(PRIVATE_DESTINATIONS)
    return f'''# Generated Lab Manager policy. {bridge} is dedicated to the Guacamole VM.
destroy table inet lab_guac_filter
destroy table ip lab_guac_nat
destroy table bridge lab_guac_l2
table bridge lab_guac_l2 {{
    chain vm_prerouting {{
        type filter hook prerouting priority -20; policy accept;
        meta ibrname "{bridge}" ether saddr != {vm_mac.lower()} drop
    }}
    chain vm_forward {{
        type filter hook forward priority -20; policy accept;
        meta ibrname "{bridge}" drop
    }}
}}
table inet lab_guac_filter {{
    chain vm_input {{
        type filter hook input priority -11; policy accept;
        iifname "{bridge}" ip saddr {address} ct state established,related accept
        iifname "{bridge}" drop
    }}
    chain vm_output {{
        type filter hook output priority -11; policy accept;
        oifname "{bridge}" ip daddr {address} tcp dport 22 accept
        oifname "{bridge}" ip daddr {address} ct state established,related accept
        oifname "{bridge}" drop
    }}
    chain vm_forward {{
        type filter hook forward priority -11; policy accept;
        iifname "{bridge}" ip saddr {address} ip daddr {pool} tcp dport 22 accept
        iifname "{bridge}" ip saddr {address} ip daddr {{ {private} }} drop
        iifname "{bridge}" ip saddr {address} oifname "{uplink}" accept
        iifname "{bridge}" drop
        oifname "{bridge}" ip daddr {address} ct state established,related accept
        oifname "{bridge}" drop
    }}
}}
table ip lab_guac_nat {{
    chain vm_postrouting {{
        type nat hook postrouting priority 100; policy accept;
        ip saddr {address} oifname "{uplink}" masquerade
    }}
}}
'''


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("vm_ipv4")
    parser.add_argument("vm_mac")
    parser.add_argument("bridge")
    parser.add_argument("uplink")
    parser.add_argument("lab_pool")
    args = parser.parse_args()
    print(render(args.vm_ipv4, args.bridge, args.uplink, args.lab_pool), end="")


if __name__ == "__main__":
    main()
