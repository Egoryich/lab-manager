from dataclasses import replace

from lab_manager.capacity import (
    GIB,
    MIB,
    CapacityPolicy,
    NodeCapacityInput,
    ResourceDemand,
    StorageObservation,
    assess_capacity,
)


def prepared_node():
    return NodeCapacityInput(
        memory_total_bytes=31993 * MIB,
        memory_free_bytes=29200 * MIB,
        logical_cpus=24,
        external_running_memory_mib=0,
        external_cpu_millicredits=0,
        storage=StorageObservation(
            name="student-lvm",
            total_bytes=456 * GIB,
            available_bytes=455 * GIB,
            thin_metadata_percent=0.39,
            active=True,
            is_thin=True,
            commitments_reconciled=True,
            external_committed_bytes=10 * GIB,
        ),
        admission_ready=True,
        inventory_fresh=True,
        ownership_reconciled=True,
    )


def policy():
    return CapacityPolicy(
        host_reserve_mib=4096,
        infrastructure_reserve_mib=2048,
        safety_reserve_mib=2048,
        cpu_millicredits_per_logical_cpu=1500,
    )


def test_small_lxc_class_fits_but_larger_ram_or_second_class_does_not():
    node = prepared_node()
    lesson = ResourceDemand(
        memory_mib=30 * 512 + 2048,
        cpu_millicredits=31000,
        disk_bytes=320 * GIB,
    )
    first = assess_capacity(node, policy(), ResourceDemand(), lesson)
    assert first.permitted
    assert first.storage_headroom_bytes < 456 * GIB
    assert (
        "RAM_INSUFFICIENT"
        in assess_capacity(
            node, policy(), ResourceDemand(), replace(lesson, memory_mib=30 * 1024 + 2048)
        ).reasons
    )
    # The second teacher must see the first teacher's locked ledger commitment.
    second = assess_capacity(node, policy(), lesson, lesson)
    assert not second.permitted
    assert {"RAM_INSUFFICIENT", "CPU_INSUFFICIENT", "STORAGE_INSUFFICIENT"} <= set(second.reasons)


def test_missing_reconciliation_or_thin_metadata_blocks_even_a_tiny_request():
    node = prepared_node()
    request = ResourceDemand(memory_mib=512, cpu_millicredits=1000, disk_bytes=GIB)
    missing = replace(
        node,
        ownership_reconciled=False,
        storage=replace(node.storage, thin_metadata_percent=None, commitments_reconciled=False),
    )
    decision = assess_capacity(missing, policy(), ResourceDemand(), request)
    assert not decision.permitted
    assert {"GUEST_OWNERSHIP_UNKNOWN", "DISK_COMMITMENTS_UNKNOWN", "THIN_METADATA_UNSAFE"} <= set(
        decision.reasons
    )
    assert not assess_capacity(
        replace(node, inventory_fresh=False), policy(), ResourceDemand(), request
    ).permitted
    assert (
        "NODE_NOT_ADMISSION_READY"
        in assess_capacity(
            replace(node, admission_ready=False), policy(), ResourceDemand(), request
        ).reasons
    )


def test_storage_floor_includes_hibernation_and_external_logical_disks():
    node = prepared_node()
    request = ResourceDemand(
        memory_mib=1024,
        cpu_millicredits=1000,
        disk_bytes=382 * GIB,
        hibernation_bytes=20 * GIB,
    )
    decision = assess_capacity(node, policy(), ResourceDemand(), request)
    assert "STORAGE_INSUFFICIENT" in decision.reasons
    assert 400 * GIB < decision.storage_headroom_bytes < 401 * GIB
    # Ending a lesson releases compute, but its existing disk still consumes capacity.
    stopped_environment = ResourceDemand(disk_bytes=390 * GIB)
    next_lesson = ResourceDemand(memory_mib=512, cpu_millicredits=1000, disk_bytes=15 * GIB)
    after_stop = assess_capacity(node, policy(), stopped_environment, next_lesson)
    assert after_stop.reasons == ("STORAGE_INSUFFICIENT",)
