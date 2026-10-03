"""Administrator-selected node limits; observations remain read-only facts."""

import uuid

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from lab_manager.audit import audit
from lab_manager.dependencies import DB, Actor, Problem, require_role
from lab_manager.nodes import NodeObservation
from lab_manager.reservation_models import (
    EnvironmentDiskAllocation,
    LessonReservation,
    NodeResourceLedger,
    NodeResourcePolicy,
)

router = APIRouter(tags=["nodes"])


class NodePolicyInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    storage_name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    host_reserve_mib: int = Field(ge=0, le=1048576)
    infrastructure_reserve_mib: int = Field(ge=0, le=1048576)
    safety_reserve_mib: int = Field(ge=0, le=1048576)
    cpu_millicredits_per_logical_cpu: int = Field(ge=1, le=4000)
    storage_free_percent: int = Field(ge=10, le=50)
    thin_metadata_limit_percent: int = Field(ge=1, le=99)
    expected_version: int = Field(ge=0)


class NodePolicyView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    node_id: uuid.UUID
    storage_name: str
    host_reserve_mib: int
    infrastructure_reserve_mib: int
    safety_reserve_mib: int
    cpu_millicredits_per_logical_cpu: int
    storage_free_percent: int
    thin_metadata_limit_percent: int
    version: int


@router.get("/admin/nodes/{node_id}/resource-policy", response_model=NodePolicyView)
async def get_node_policy(node_id: uuid.UUID, actor: Actor, db: DB):
    require_role(actor, "ADMIN")
    policy = await db.get(NodeResourcePolicy, node_id)
    if policy is None:
        raise Problem(404, "NOT_FOUND", "Политика ресурсов узла ещё не настроена.")
    return policy


@router.put("/admin/nodes/{node_id}/resource-policy", response_model=NodePolicyView)
async def put_node_policy(
    node_id: uuid.UUID, body: NodePolicyInput, actor: Actor, db: DB, request: Request
):
    require_role(actor, "ADMIN")
    # Same first lock as reserve_lesson: a policy edit cannot race admission.
    ledger = await db.scalar(
        select(NodeResourceLedger).where(NodeResourceLedger.node_id == node_id).with_for_update()
    )
    node = await db.scalar(
        select(NodeObservation).where(NodeObservation.id == node_id).with_for_update()
    )
    if node is None or node.payload is None:
        raise Problem(404, "NOT_FOUND", "Узел с полученным снимком не найден.")
    storages = node.payload.get("sample", {}).get("storages", [])
    matching = [storage for storage in storages if storage.get("name") == body.storage_name]
    if len(matching) != 1 or not matching[0].get("active"):
        raise Problem(409, "STORAGE_UNAVAILABLE", "Выбранное хранилище не доступно на узле.")
    if matching[0].get("backend") != "lvmthin":
        raise Problem(409, "STORAGE_UNSUPPORTED", "Тип хранилища пока не поддерживается.")
    policy = await db.get(NodeResourcePolicy, node_id)
    actual_version = policy.version if policy else 0
    if body.expected_version != actual_version:
        raise Problem(409, "VERSION_CONFLICT", "Политика изменилась. Обновите страницу.")
    if policy is not None:
        in_use = await db.scalar(
            select(LessonReservation.id)
            .where(
                LessonReservation.node_id == node_id,
                LessonReservation.state.in_(("RESERVED", "ACTIVE")),
            )
            .limit(1)
        )
        if in_use is not None:
            raise Problem(409, "POLICY_IN_USE", "Сначала завершите действующие брони узла.")
    if policy is not None and policy.storage_name != body.storage_name:
        allocated = await db.scalar(
            select(EnvironmentDiskAllocation.environment_id)
            .where(EnvironmentDiskAllocation.node_id == node_id)
            .limit(1)
        )
        if allocated is not None:
            raise Problem(409, "STORAGE_IN_USE", "Для узла уже учтены диски окружений.")
    fields = body.model_dump(exclude={"expected_version"})
    if policy is None:
        policy = NodeResourcePolicy(node_id=node_id, **fields)
        db.add(policy)
        await db.flush()
        if ledger is None:
            db.add(NodeResourceLedger(node_id=node_id))
    else:
        for key, value in fields.items():
            setattr(policy, key, value)
        policy.version += 1
    audit(db, request, actor.id, "node.resource_policy_saved", node_id, version=policy.version)
    await db.commit()
    return policy
