"""Conservative resource arithmetic for a future transactional scheduler.

An estimate is never a reservation. Admission callers must hold the node ledger
lock, read a fresh reconciled inventory, and persist the reservation atomically.
"""

from dataclasses import dataclass

MIB = 2**20
GIB = 2**30


@dataclass(frozen=True)
class CapacityPolicy:
    host_reserve_mib: int
    infrastructure_reserve_mib: int
    safety_reserve_mib: int
    cpu_millicredits_per_logical_cpu: int
    storage_free_percent: int = 10
    thin_metadata_limit_percent: int = 80

    def __post_init__(self):
        if (
            min(
                self.host_reserve_mib,
                self.infrastructure_reserve_mib,
                self.safety_reserve_mib,
                self.cpu_millicredits_per_logical_cpu,
            )
            < 0
            or not 1 <= self.cpu_millicredits_per_logical_cpu <= 4000
        ):
            raise ValueError("Invalid CPU or RAM reserve")
        if not 1 <= self.storage_free_percent <= 50:
            raise ValueError("Invalid storage free floor")
        if not 1 <= self.thin_metadata_limit_percent <= 99:
            raise ValueError("Invalid thin metadata limit")


@dataclass(frozen=True)
class ResourceDemand:
    memory_mib: int = 0
    cpu_millicredits: int = 0
    disk_bytes: int = 0
    hibernation_bytes: int = 0

    def __post_init__(self):
        if (
            min(
                self.memory_mib,
                self.cpu_millicredits,
                self.disk_bytes,
                self.hibernation_bytes,
            )
            < 0
        ):
            raise ValueError("Negative resource demand")

    @property
    def storage_bytes(self):
        return self.disk_bytes + self.hibernation_bytes


@dataclass(frozen=True)
class StorageObservation:
    name: str
    total_bytes: int | None
    available_bytes: int | None
    thin_metadata_percent: float | None
    active: bool
    is_thin: bool
    commitments_reconciled: bool
    external_committed_bytes: int = 0


@dataclass(frozen=True)
class NodeCapacityInput:
    memory_total_bytes: int
    memory_free_bytes: int
    logical_cpus: int
    external_running_memory_mib: int
    external_cpu_millicredits: int
    storage: StorageObservation
    admission_ready: bool
    inventory_fresh: bool
    ownership_reconciled: bool


@dataclass(frozen=True)
class CapacityDecision:
    permitted: bool
    reasons: tuple[str, ...]
    ram_headroom_mib: int
    cpu_headroom_millicredits: int
    storage_headroom_bytes: int


def assess_capacity(
    node: NodeCapacityInput,
    policy: CapacityPolicy,
    committed: ResourceDemand,
    requested: ResourceDemand,
) -> CapacityDecision:
    """Bound simultaneous allocation; missing evidence fails closed.

    `committed` is assembled under a ledger lock. RAM/CPU include overlapping
    lessons; disk/hibernation include persistent environments even when stopped.
    Neither sum is inferred from physical thin-pool usage or guest maxdisk.
    """
    reasons = []
    if not node.admission_ready:
        reasons.append("NODE_NOT_ADMISSION_READY")
    if not node.inventory_fresh:
        reasons.append("INVENTORY_STALE")
    if not node.ownership_reconciled:
        reasons.append("GUEST_OWNERSHIP_UNKNOWN")
    if (
        min(
            node.memory_total_bytes,
            node.memory_free_bytes,
            node.logical_cpus,
            node.external_running_memory_mib,
            node.external_cpu_millicredits,
            node.storage.external_committed_bytes,
        )
        < 0
        or node.logical_cpus == 0
        or node.memory_free_bytes > node.memory_total_bytes
    ):
        reasons.append("INVENTORY_INVALID")

    ram_budget = max(
        0,
        node.memory_total_bytes // MIB
        - policy.host_reserve_mib
        - policy.infrastructure_reserve_mib
        - policy.safety_reserve_mib
        - node.external_running_memory_mib,
    )
    ram_headroom = max(0, ram_budget - committed.memory_mib)
    # A second check against currently free RAM protects against host memory
    # used by unknown processes even when the configured budget is generous.
    free_ram_headroom = max(0, node.memory_free_bytes // MIB - policy.safety_reserve_mib)
    if requested.memory_mib > min(ram_headroom, free_ram_headroom):
        reasons.append("RAM_INSUFFICIENT")

    cpu_budget = max(
        0,
        node.logical_cpus * policy.cpu_millicredits_per_logical_cpu
        - node.external_cpu_millicredits,
    )
    cpu_headroom = max(0, cpu_budget - committed.cpu_millicredits)
    if requested.cpu_millicredits > cpu_headroom:
        reasons.append("CPU_INSUFFICIENT")

    storage = node.storage
    if not storage.active or storage.total_bytes is None or storage.available_bytes is None:
        reasons.append("STORAGE_UNAVAILABLE")
        storage_headroom = 0
    elif (
        storage.total_bytes <= 0
        or storage.available_bytes < 0
        or storage.available_bytes > storage.total_bytes
    ):
        reasons.append("STORAGE_INVALID")
        storage_headroom = 0
    else:
        floor = (storage.total_bytes * policy.storage_free_percent + 99) // 100
        logical_headroom = max(
            0,
            storage.total_bytes
            - floor
            - storage.external_committed_bytes
            - committed.storage_bytes,
        )
        physical_headroom = max(0, storage.available_bytes - floor)
        storage_headroom = min(logical_headroom, physical_headroom)
        if requested.storage_bytes > storage_headroom:
            reasons.append("STORAGE_INSUFFICIENT")
    if not storage.commitments_reconciled:
        reasons.append("DISK_COMMITMENTS_UNKNOWN")
    if storage.is_thin and (
        storage.thin_metadata_percent is None
        or not 0 <= storage.thin_metadata_percent < policy.thin_metadata_limit_percent
    ):
        reasons.append("THIN_METADATA_UNSAFE")
    return CapacityDecision(
        permitted=not reasons,
        reasons=tuple(reasons),
        ram_headroom_mib=min(ram_headroom, free_ram_headroom),
        cpu_headroom_millicredits=cpu_headroom,
        storage_headroom_bytes=storage_headroom,
    )
