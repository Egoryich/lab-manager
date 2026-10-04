"""Last observations are diagnostic facts, never admission budgets or power state."""

import asyncio
import uuid
from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import DateTime, String, func, select
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.orm import Mapped, mapped_column

from lab_manager.dependencies import DB, Actor, require_role
from lab_manager.models import Base
from lab_manager.node_reconciliation import reconcile
from lab_manager.node_transport import BridgeObservation, HostObservation, NodeTransportError, fetch
from lab_manager.runtime_models import ProviderRuntimeBinding, RuntimeDisk


class NodeObservation(Base):
    __tablename__ = "node_observations"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    attempt_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_contact_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sample_finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(64))


class NodeView(BaseModel):
    id: uuid.UUID
    name: str
    status: str
    last_contact_at: datetime | None
    sampled_at: datetime | None
    error_code: str | None
    host: HostObservation | None
    guest_count: int | None
    storage_count: int | None
    storages: list["StorageView"] | None
    network_bridges: list[BridgeObservation] | None
    admission_ready: bool = False


class StorageView(BaseModel):
    name: str
    backend: str
    active: bool
    total_bytes: int | None
    used_bytes: int | None
    available_bytes: int | None
    thin_metadata_percent: float | None
    observed_volume_count: int | None
    physical_volumes: list[str] | None
    physical_backing_reconciled: bool | None


def storage_view(item: dict, local_pools: dict) -> StorageView:
    pool = local_pools.get(item["name"])
    backing = pool.get("backing") if pool else None
    return StorageView(
        **item,
        observed_volume_count=len(pool["volumes"]) if pool else None,
        physical_volumes=[device["name"] for device in backing["physical_volumes"]]
        if backing
        else None,
        physical_backing_reconciled=backing["physical_backing_reconciled"] if backing else None,
    )


async def store_observation(
    sessions, endpoint, started, *, payload=None, sampled_at=None, error=None
):
    async with sessions() as db, db.begin():
        now = await db.scalar(select(func.clock_timestamp()))
        values = dict(
            id=endpoint.id, name=endpoint.name, attempt_started_at=started, error_code=error
        )
        if payload is not None:
            values.update(last_contact_at=now, sample_finished_at=sampled_at, payload=payload)
        # Failure preserves the last successful sample. A later-started poll wins races.
        updates = {key: value for key, value in values.items() if key != "id"}
        await db.execute(
            insert(NodeObservation)
            .values(**values)
            .on_conflict_do_update(
                index_elements=[NodeObservation.id],
                set_=updates,
                where=NodeObservation.attempt_started_at < started,
            )
        )


async def poll_node(sessions, endpoint):
    async with sessions() as db:
        started = await db.scalar(select(func.clock_timestamp()))
    try:
        payload, sampled_at = await asyncio.to_thread(fetch, endpoint)
    except NodeTransportError as error:
        await store_observation(sessions, endpoint, started, error=str(error))
    else:
        async with sessions() as db:
            bindings = list(
                await db.scalars(
                    select(ProviderRuntimeBinding).where(
                        ProviderRuntimeBinding.node_id == endpoint.id
                    )
                )
            )
            disks = list(
                await db.scalars(select(RuntimeDisk).where(RuntimeDisk.node_id == endpoint.id))
            )
        payload["sample"] = reconcile(payload["sample"], bindings, disks)
        await store_observation(sessions, endpoint, started, payload=payload, sampled_at=sampled_at)


async def poll_forever(sessions, endpoints, stop):
    while not stop.is_set():
        for endpoint in endpoints:
            if stop.is_set():
                return
            try:
                await poll_node(sessions, endpoint)
            except Exception:
                # DB failures do not kill the operation worker or expose credentials.
                import logging

                logging.getLogger(__name__).warning("node_poll_failed node_id=%s", endpoint.id)
        try:
            await asyncio.wait_for(stop.wait(), 30)
        except TimeoutError:
            pass


router = APIRouter(tags=["nodes"])


@router.get("/admin/nodes", response_model=list[NodeView])
async def list_nodes(db: DB, actor: Actor):
    require_role(actor, "ADMIN")
    now = await db.scalar(select(func.clock_timestamp()))
    result = []
    for row in await db.scalars(select(NodeObservation).order_by(NodeObservation.name)):
        sample = row.payload["sample"] if row.payload else None
        fresh = (
            row.sample_finished_at is not None
            and row.last_contact_at is not None
            and -10 <= (now - row.sample_finished_at).total_seconds() <= 120
            and 0 <= (now - row.last_contact_at).total_seconds() <= 120
        )
        local_pools = (
            {item["storage"]: item for item in sample.get("local_thin_pools", [])} if sample else {}
        )
        storages = (
            [storage_view(item, local_pools) for item in sample["storages"]] if sample else None
        )
        result.append(
            NodeView(
                id=row.id,
                name=row.name,
                status="FRESH"
                if fresh and not row.error_code
                else "STALE"
                if sample
                else "UNKNOWN",
                last_contact_at=row.last_contact_at,
                sampled_at=row.sample_finished_at,
                error_code=row.error_code,
                host=sample["host"] if sample else None,
                guest_count=len(sample["guests"]) if sample else None,
                storage_count=len(sample["storages"]) if sample else None,
                storages=storages,
                network_bridges=sample.get("network_bridges")
                if sample and "NETWORK_INVENTORY_UNAVAILABLE" not in sample["limitations"]
                else None,
            )
        )
    return result
