import importlib.util
from pathlib import Path

import pytest


def renderer():
    path = Path(__file__).resolve().parents[2] / "tools" / "render-guacamole-egress.py"
    spec = importlib.util.spec_from_file_location("guacamole_egress", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.render


def test_only_guacamole_vm_can_reach_public_uplink_and_guest_ssh():
    rules = renderer()("10.60.0.10", "bc:24:11:00:00:01", "vmbr1", "vmbr0", "10.70.0.0/16")
    assert (
        'iifname "vmbr1" ip saddr 10.60.0.10 ip daddr 10.70.0.0/16 tcp dport 22 accept'
    ) in rules
    assert rules.index("tcp dport 22 accept") < rules.index("10.0.0.0/8")
    assert 'iifname "vmbr1" ip saddr 10.60.0.10 oifname "vmbr0" accept' in rules
    assert 'meta ibrname "vmbr1" ether saddr != bc:24:11:00:00:01 drop' in rules
    assert 'meta ibrname "vmbr1" drop' in rules
    assert 'oifname "vmbr1" ip daddr 10.60.0.10 tcp dport 22 accept' in rules
    assert 'iifname "vmbr1" drop' in rules
    assert 'oifname "vmbr1" drop' in rules
    assert 'ip saddr 10.60.0.10 oifname "vmbr0" masquerade' in rules


@pytest.mark.parametrize(
    "arguments",
    [
        ("10.60.0.10", "bc:24:11:00:00:01", "vmbr0", "vmbr0", "10.70.0.0/16"),
        ("10.60.0.10", "bc:24:11:00:00:01", "vmbr1;flush", "vmbr0", "10.70.0.0/16"),
        ("10.60.0.10", "bc:24:11:00:00:01", "vmbr1", "vmbr0", "192.168.0.0/24"),
        ("8.8.8.8", "bc:24:11:00:00:01", "vmbr1", "vmbr0", "10.70.0.0/16"),
        ("10.60.0.10", "invalid-mac", "vmbr1", "vmbr0", "10.70.0.0/16"),
    ],
)
def test_rejects_untrusted_egress_configuration(arguments):
    with pytest.raises(ValueError, match="INVALID_GUACAMOLE_EGRESS_CONFIG"):
        renderer()(*arguments)
