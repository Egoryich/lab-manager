import asyncio
import uuid

import pytest
from lab_manager.catalog_models import (
    Environment,
    PermissionPolicyRevision,
    ProfileVersion,
    TemplateVersion,
)
from lab_manager.models import Group
from lab_manager.network_allocations import NetworkAllocationRejected, reserve_network_segment
from lab_manager.network_models import NetworkSegmentAllocation, NodeNetworkPool
from lab_manager.nodes import NodeObservation
from sqlalchemy import func, select

pytestmark = pytest.mark.integration


async def setup(app, teacher_id):
    node_id = uuid.uuid4()
    async with app.state.sessions() as db, db.begin():
        now = await db.scalar(select(func.clock_timestamp()))
        db.add(
            NodeObservation(
                id=node_id,
                name="Synthetic network node",
                attempt_started_at=now,
            )
        )
        await db.flush()
        db.add(NodeNetworkPool(node_id=node_id, cidr="10.70.0.0/29"))
        group = Group(name="Network test", owner_teacher_id=teacher_id)
        template = TemplateVersion(
            name="Linux",
            version_label="v1",
            runtime_kind="LXC",
            guest_family="LINUX",
            created_by=teacher_id,
        )
        policy = PermissionPolicyRevision(name="Network test", created_by=teacher_id)
        db.add_all((group, template, policy))
        await db.flush()
        profile = ProfileVersion(
            name="Small",
            template_version_id=template.id,
            memory_mib=512,
            vcpu=1,
            cpu_millicredits=1000,
            disk_gib=10,
            min_memory_mib=512,
            max_memory_mib=512,
            min_vcpu=1,
            max_vcpu=1,
            min_disk_gib=10,
            max_disk_gib=10,
            network_mode="ISOLATED",
            internet_enabled=False,
            created_by=teacher_id,
        )
        db.add(profile)
        await db.flush()
        environment = Environment(
            name="Network test",
            group_id=group.id,
            owner_teacher_id=teacher_id,
            profile_version_id=profile.id,
            demo_profile_version_id=profile.id,
            student_memory_mib=512,
            student_vcpu=1,
            student_disk_gib=10,
            demo_memory_mib=512,
            demo_vcpu=1,
            demo_disk_gib=10,
            permission_revision_id=policy.id,
            request_id=uuid.uuid4(),
        )
        db.add(environment)
        await db.flush()
        return node_id, environment.id


async def test_parallel_claims_are_unique_and_idempotent(app, seed):
    teacher = await seed("TEACHER")
    node_id, environment_id = await setup(app, teacher.id)
    keys = (uuid.uuid4(), uuid.uuid4(), uuid.uuid4())

    async def claim(key):
        return await reserve_network_segment(
            app.state.sessions,
            node_id=node_id,
            environment_id=environment_id,
            teacher_id=teacher.id,
            segment_key=key,
            mode="ISOLATED",
            requested_hosts=2,
        )

    first, second = await asyncio.gather(claim(keys[0]), claim(keys[1]))
    assert {first.cidr, second.cidr} == {"10.70.0.0/30", "10.70.0.4/30"}
    assert (await claim(keys[0])).id == first.id
    with pytest.raises(NetworkAllocationRejected, match="NETWORK_POOL_EXHAUSTED"):
        await claim(keys[2])
    with pytest.raises(NetworkAllocationRejected, match="SEGMENT_KEY_CONFLICT"):
        await reserve_network_segment(
            app.state.sessions,
            node_id=node_id,
            environment_id=environment_id,
            teacher_id=teacher.id,
            segment_key=keys[0],
            mode="GROUP_LAN",
            requested_hosts=2,
        )
    async with app.state.sessions() as db, db.begin():
        row = await db.get(NetworkSegmentAllocation, first.id)
        row.state = "RELEASING"
    with pytest.raises(NetworkAllocationRejected, match="NETWORK_POOL_EXHAUSTED"):
        await claim(keys[2])
    async with app.state.sessions() as db, db.begin():
        row = await db.get(NetworkSegmentAllocation, first.id)
        row.state = "RELEASED"  # Models the later, agent-confirmed detach.
        row.released_at = await db.scalar(select(func.clock_timestamp()))
    reused = await claim(keys[2])
    assert reused.cidr == first.cidr
    with pytest.raises(NetworkAllocationRejected, match="SEGMENT_KEY_CONFLICT"):
        await claim(keys[0])
    async with app.state.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(NetworkSegmentAllocation)) == 3


async def test_teacher_cannot_claim_other_environment(app, seed):
    owner = await seed("TEACHER")
    other = await seed("TEACHER")
    node_id, environment_id = await setup(app, owner.id)
    with pytest.raises(NetworkAllocationRejected, match="ENVIRONMENT_NOT_OWNED"):
        await reserve_network_segment(
            app.state.sessions,
            node_id=node_id,
            environment_id=environment_id,
            teacher_id=other.id,
            segment_key=uuid.uuid4(),
            mode="ISOLATED",
            requested_hosts=2,
        )
    async with app.state.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(NetworkSegmentAllocation)) == 0
