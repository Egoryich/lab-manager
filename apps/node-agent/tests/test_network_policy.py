import json
import sys

import pytest

from lab_node_agent.network_policy import (
    IsolatedSshGuest,
    Segment,
    SegmentMode,
    main,
    render_isolated_ssh_policy,
    render_l2_policy,
)


def test_empty_policy_keeps_all_guest_paths_closed():
    rules = render_l2_policy(())
    assert rules.count('meta ibrname "lmbr*" drop') == 2
    assert 'iifname "lmbr*" drop' in rules
    assert 'oifname "lmbr*" drop' in rules


def test_only_group_bridge_gets_l2_forwarding():
    rules = render_l2_policy(
        (
            Segment("lmbrgroup1", SegmentMode.GROUP_LAN),
            Segment("lmbrsolo1", SegmentMode.ISOLATED),
        )
    )
    assert 'meta ibrname "lmbrgroup1" accept' in rules
    assert "lmbrsolo1" not in rules
    assert rules.index('meta ibrname "lmbrgroup1" accept') < rules.index(
        'meta ibrname "lmbr*" drop', rules.index("chain guest_bridge_forward")
    )


@pytest.mark.parametrize(
    "segments",
    [
        (Segment('lmbrx" accept', SegmentMode.GROUP_LAN),),
        (Segment("vmbr0", SegmentMode.GROUP_LAN),),
        (Segment("lmbrtoo-long-bridge", SegmentMode.GROUP_LAN),),
        (Segment("lmbrx", "GROUP_LAN"),),
        (Segment("lmbrx", SegmentMode.GROUP_LAN), Segment("lmbrx", SegmentMode.ISOLATED)),
    ],
)
def test_invalid_policy_is_rejected(segments):
    with pytest.raises(ValueError):
        render_l2_policy(segments)


def test_isolated_guest_opens_only_guacamole_ssh_and_its_validated_return():
    guest = IsolatedSshGuest("lmbrsolo1", 901001, "10.70.2.0/30", "10.70.2.2", "BC:24:11:AA:BB:CC")
    rules = render_isolated_ssh_policy(
        (Segment("lmbrsolo1", SegmentMode.ISOLATED),),
        (guest,),
        guacamole_ipv4="10.60.0.10",
        guacamole_bridge="vmbr1",
    )
    assert (
        'iifname "vmbr1" ip saddr 10.60.0.10 oifname "lmbrsolo1" '
        "ip daddr 10.70.2.2 tcp dport 22 ct state new,established accept"
    ) in rules
    assert (
        'iifname "lmbrsolo1" ip saddr 10.70.2.2 oifname "vmbr1" '
        "ip daddr 10.60.0.10 tcp sport 22 ct state established,related accept"
    ) in rules
    assert (
        'meta ibrname "lmbrsolo1" iifname "veth901001i0" ether saddr bc:24:11:aa:bb:cc '
        "arp saddr ip 10.70.2.2 arp daddr ip 10.70.2.1 accept"
    ) in rules
    assert 'meta ibrname "lmbr*" drop' in rules
    assert 'iifname "lmbr*" drop' in rules
    assert 'oifname "lmbr*" drop' in rules
    assert 'meta ibrname "lmbrsolo1" accept' not in rules
    assert "masquerade" not in rules


@pytest.mark.parametrize(
    "guest",
    [
        IsolatedSshGuest("vmbr0", 901001, "10.70.2.0/30", "10.70.2.2", "BC:24:11:AA:BB:CC"),
        IsolatedSshGuest("lmbrsolo1", 901001, "10.70.2.0/29", "10.70.2.2", "BC:24:11:AA:BB:CC"),
        IsolatedSshGuest("lmbrsolo1", 901001, "10.70.2.0/30", "10.70.2.1", "BC:24:11:AA:BB:CC"),
        IsolatedSshGuest("lmbrsolo1", 901001, "10.70.2.0/30", "10.70.2.2", "BD:24:11:AA:BB:CC"),
        IsolatedSshGuest("lmbrsolo1", 901001, "10.70.2.0/30", "10.70.2.2", 'aa" drop'),
    ],
)
def test_invalid_isolated_guest_is_denied(guest):
    with pytest.raises(ValueError):
        render_isolated_ssh_policy(
            (Segment("lmbrsolo1", SegmentMode.ISOLATED),),
            (guest,),
            guacamole_ipv4="10.60.0.10",
            guacamole_bridge="vmbr1",
        )


def test_shared_bridge_and_duplicate_guest_are_not_admitted():
    guest = IsolatedSshGuest("lmbrsolo1", 901001, "10.70.2.0/30", "10.70.2.2", "BC:24:11:AA:BB:CC")
    for segments, guests in (
        ((Segment("lmbrsolo1", SegmentMode.GROUP_LAN),), (guest,)),
        ((Segment("lmbrsolo1", SegmentMode.ISOLATED),), (guest, guest)),
    ):
        with pytest.raises(ValueError):
            render_isolated_ssh_policy(
                segments,
                guests,
                guacamole_ipv4="10.60.0.10",
                guacamole_bridge="vmbr1",
            )


def test_cli_renders_typed_ssh_admission(tmp_path, monkeypatch, capsys):
    config = tmp_path / "network.json"
    config.write_text(
        json.dumps(
            {
                "segments": [{"bridge": "lmbrsolo1", "mode": "ISOLATED"}],
                "guacamole": {"address": "10.60.0.10", "bridge": "vmbr1"},
                "guests": [
                    {
                        "bridge": "lmbrsolo1",
                        "vmid": 901001,
                        "cidr": "10.70.2.0/30",
                        "address": "10.70.2.2",
                        "mac": "BC:24:11:AA:BB:CC",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "argv", ["lab-node-policy", str(config)])
    assert main() == 0
    assert 'iifname "vmbr1" ip saddr 10.60.0.10' in capsys.readouterr().out
