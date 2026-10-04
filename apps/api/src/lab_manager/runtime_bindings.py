"""Reserve Proxmox VMIDs for a prepared lesson without calling Proxmox.

The node ledger serializes all Lab Manager allocations. Inventory is checked
before choosing an ID; the provider must still reject an externally claimed
ID and require the exact ownership marker before any later operation.
"""

import uuid

from sqlalchemy import func, select, text

from lab_manager.capacity import GIB
from lab_manager.nodes import NodeObservation
from lab_manager.reservation_models import NodeResourceLedger, NodeResourcePolicy
from lab_manager.runtime_models import (
    EnvironmentRun,
    ProviderRuntimeBinding,
    RunRuntime,
    Runtime,
    RuntimeDisk,
)

VMID_START = 900000
VMID_END = 999999


class RuntimeBindingRejected(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def observed_vmids(observation: NodeObservation) -> set[int]:
    try:
        guests = observation.payload["sample"]["guests"]
        values = [guest["vmid"] for guest in guests]
        if not isinstance(guests, list) or any(
            type(value) is not int or not 100 <= value <= 999999999 for value in values
        ):
            raise ValueError("invalid guest VMID")
        if len(values) != len(set(values)):
            raise ValueError("duplicate guest VMID")
        return set(values)
    except (TypeError, KeyError, ValueError) as error:
        raise RuntimeBindingRejected("GUEST_INVENTORY_INVALID") from error


async def bind_prepared_runtimes(sessions, *, run_id: uuid.UUID) -> list[ProviderRuntimeBinding]:
    """Bind every runtime in one transaction; retries return the same VMIDs."""
    async with sessions() as db, db.begin():
        await db.execute(text("SET LOCAL statement_timeout = '10s'"))
        await db.execute(text("SET LOCAL lock_timeout = '5s'"))
        identity = await db.get(EnvironmentRun, run_id)
        if identity is None:
            raise RuntimeBindingRejected("RUN_NOT_FOUND")
        ledger = await db.scalar(
            select(NodeResourceLedger)
            .where(NodeResourceLedger.node_id == identity.node_id)
            .with_for_update()
        )
        if ledger is None:
            raise RuntimeBindingRejected("NODE_LEDGER_MISSING")
        run = await db.scalar(
            select(EnvironmentRun).where(EnvironmentRun.id == run_id).with_for_update()
        )
        await db.refresh(run, with_for_update=True)
        if run.state != "PREPARING":
            raise RuntimeBindingRejected("RUN_NOT_PREPARING")
        runtimes = list(
            await db.scalars(
                select(Runtime)
                .join(RunRuntime, RunRuntime.runtime_id == Runtime.id)
                .where(RunRuntime.run_id == run_id)
                .order_by(Runtime.id)
                .with_for_update(of=Runtime)
            )
        )
        if not runtimes or any(runtime.node_id != run.node_id for runtime in runtimes):
            raise RuntimeBindingRejected("RUN_ROSTER_INVALID")
        policy = await db.get(NodeResourcePolicy, run.node_id)
        if policy is None:
            raise RuntimeBindingRejected("NODE_POLICY_MISSING")
        bindings = list(
            await db.scalars(
                select(ProviderRuntimeBinding).where(ProviderRuntimeBinding.node_id == run.node_id)
            )
        )
        by_runtime = {binding.runtime_id: binding for binding in bindings}
        if len(by_runtime) != len(bindings):
            raise RuntimeBindingRejected("BINDING_DUPLICATE")
        disks = list(
            await db.scalars(
                select(RuntimeDisk).where(
                    RuntimeDisk.runtime_id.in_(runtime.id for runtime in runtimes)
                )
            )
        )
        by_disk_runtime = {disk.runtime_id: disk for disk in disks}
        if len(by_disk_runtime) != len(disks):
            raise RuntimeBindingRejected("RUNTIME_DISK_DUPLICATE")
        result = []
        pending = []
        for runtime in runtimes:
            binding = by_runtime.get(runtime.id)
            disk = by_disk_runtime.get(runtime.id)
            marker = f"lab-manager:runtime={runtime.id};generation={runtime.generation}"
            if binding is not None:
                if (
                    binding.generation != runtime.generation
                    or binding.ownership_marker != marker
                    or disk is None
                    or disk.node_id != run.node_id
                    or disk.storage_name != policy.storage_name
                    or disk.logical_bytes != runtime.disk_gib * GIB
                    or disk.state not in ("PLANNED", "PRESENT")
                ):
                    raise RuntimeBindingRejected("BINDING_IDENTITY_MISMATCH")
                result.append(binding)
            elif runtime.state == "PLANNED" and disk is None:
                pending.append((runtime, marker))
            else:
                raise RuntimeBindingRejected("RUNTIME_UNBOUND")
        if not pending:
            return result
        observation = await db.get(NodeObservation, run.node_id)
        now = await db.scalar(select(func.clock_timestamp()))
        if (
            observation is None
            or observation.error_code is not None
            or observation.sample_finished_at is None
            or observation.last_contact_at is None
            or not -10 <= (now - observation.sample_finished_at).total_seconds() <= 120
            or not 0 <= (now - observation.last_contact_at).total_seconds() <= 120
        ):
            raise RuntimeBindingRejected("INVENTORY_STALE")
        occupied = observed_vmids(observation) | {binding.vmid for binding in bindings}
        candidates = (vmid for vmid in range(VMID_START, VMID_END + 1) if vmid not in occupied)
        for runtime, marker in pending:
            vmid = next(candidates, None)
            if vmid is None:
                raise RuntimeBindingRejected("VMID_POOL_EXHAUSTED")
            occupied.add(vmid)
            binding = ProviderRuntimeBinding(
                runtime_id=runtime.id,
                node_id=run.node_id,
                vmid=vmid,
                ownership_marker=marker,
                generation=runtime.generation,
            )
            db.add(binding)
            db.add(
                RuntimeDisk(
                    runtime_id=runtime.id,
                    node_id=run.node_id,
                    storage_name=policy.storage_name,
                    provider_ref=None,
                    logical_bytes=runtime.disk_gib * GIB,
                    observed_physical_bytes=0,
                    state="PLANNED",
                )
            )
            result.append(binding)
        await db.flush()
        return result
