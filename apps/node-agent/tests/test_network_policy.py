import pytest

from lab_node_agent.network_policy import Segment, SegmentMode, render_l2_policy


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
