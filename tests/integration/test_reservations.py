import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from lab_manager.capacity import GIB, MIB, ResourceDemand
from lab_manager.catalog_models import (
    Environment,
    PermissionPolicyRevision,
    ProfileVersion,
    TemplateVersion,
)
from lab_manager.models import Group
from lab_manager.nodes import NodeObservation
from lab_manager.reservation_models import (
    EnvironmentDiskAllocation,
    LessonReservation,
    NodeResourceLedger,
    NodeResourcePolicy,
)
from lab_manager.reservations import AdmissionRejected, cancel_future_lesson, reserve_lesson
from sqlalchemy import func, select

pytestmark = pytest.mark.integration


async def setup(app, teacher_ids):
    node_id = uuid.uuid4()
    payload = {
        "sample": {
            "admission_ready": True,
            "ownership_reconciled": True,
            "external_running_memory_mib": 0,
            "external_cpu_millicredits": 0,
            "host": {
                "memory_total_bytes": 4096 * MIB,
                "memory_free_bytes": 4096 * MIB,
                "logical_cpus": 4,
            },
            "storages": [
                {
                    "name": "student-lvm",
                    "backend": "lvmthin",
                    "active": True,
                    "total_bytes": 100 * GIB,
                    "available_bytes": 100 * GIB,
                    "thin_metadata_percent": 0.5,
                    "commitments_reconciled": True,
                    "external_committed_bytes": 10 * GIB,
                }
            ],
        }
    }
    async with app.state.sessions() as db, db.begin():
        now = await db.scalar(select(func.clock_timestamp()))
        db.add(
            NodeObservation(
                id=node_id,
                name="Synthetic test node",
                attempt_started_at=now,
                last_contact_at=now,
                sample_finished_at=now,
                payload=payload,
            )
        )
        await db.flush()
        db.add(
            NodeResourcePolicy(
                node_id=node_id,
                storage_name="student-lvm",
                host_reserve_mib=1024,
                infrastructure_reserve_mib=512,
                safety_reserve_mib=512,
                cpu_millicredits_per_logical_cpu=1000,
                storage_free_percent=10,
                thin_metadata_limit_percent=80,
            )
        )
        await db.flush()
        db.add(NodeResourceLedger(node_id=node_id))
        template = TemplateVersion(
            name="Linux",
            version_label="v1",
            runtime_kind="LXC",
            guest_family="LINUX",
            created_by=teacher_ids[0],
        )
        db.add(template)
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
            created_by=teacher_ids[0],
        )
        policy = PermissionPolicyRevision(name="Test", created_by=teacher_ids[0])
        db.add_all((profile, policy))
        await db.flush()
        environment_ids = []
        for teacher_id in teacher_ids:
            group = Group(name="Test", owner_teacher_id=teacher_id)
            db.add(group)
            await db.flush()
            environment = Environment(
                name="Lesson",
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
            environment_ids.append(environment.id)
    return node_id, environment_ids


async def test_two_teachers_cannot_book_the_same_node_capacity(app, seed):
    teachers = [await seed("TEACHER") for _ in range(2)]
    node_id, environments = await setup(app, [teacher.id for teacher in teachers])
    start = datetime.now(UTC) + timedelta(hours=1)
    end = start + timedelta(hours=2)
    demand = ResourceDemand(memory_mib=1500, cpu_millicredits=2500, disk_bytes=45 * GIB)
    requests = [uuid.uuid4() for _ in teachers]

    async def book(index):
        return await reserve_lesson(
            app.state.sessions,
            node_id=node_id,
            environment_id=environments[index],
            teacher_id=teachers[index].id,
            request_id=requests[index],
            starts_at=start,
            ends_at=end,
            demand=demand,
        )

    results = await asyncio.gather(book(0), book(1), return_exceptions=True)
    assert sum(isinstance(result, LessonReservation) for result in results) == 1, [
        type(result).__name__ + ":" + str(result) for result in results
    ]
    winner = next(result for result in results if isinstance(result, LessonReservation))
    loser = next(result for result in results if isinstance(result, AdmissionRejected))
    assert "RAM_INSUFFICIENT" in loser.reasons
    async with app.state.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(LessonReservation)) == 1
        assert await db.scalar(select(func.count()).select_from(EnvironmentDiskAllocation)) == 1
    index = next(i for i, result in enumerate(results) if result is winner)
    repeat = await book(index)
    assert repeat.id == winner.id
    await cancel_future_lesson(
        app.state.sessions, reservation_id=winner.id, teacher_id=teachers[index].id
    )
    async with app.state.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(EnvironmentDiskAllocation)) == 0
    other = 1 - index
    assert isinstance(await book(other), LessonReservation)


async def test_incomplete_agent_snapshot_never_creates_a_reservation(app, seed):
    teacher = await seed("TEACHER")
    node_id, environments = await setup(app, [teacher.id])
    async with app.state.sessions() as db, db.begin():
        observation = await db.get(NodeObservation, node_id)
        payload = dict(observation.payload)
        payload["sample"] = dict(payload["sample"])
        payload["sample"]["admission_ready"] = False
        observation.payload = payload
    start = datetime.now(UTC) + timedelta(hours=1)
    with pytest.raises(AdmissionRejected) as error:
        await reserve_lesson(
            app.state.sessions,
            node_id=node_id,
            environment_id=environments[0],
            teacher_id=teacher.id,
            request_id=uuid.uuid4(),
            starts_at=start,
            ends_at=start + timedelta(hours=1),
            demand=ResourceDemand(memory_mib=512, cpu_millicredits=1000, disk_bytes=GIB),
        )
    assert "NODE_NOT_ADMISSION_READY" in error.value.reasons
    async with app.state.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(LessonReservation)) == 0


async def test_cancelling_lesson_preserves_materialized_disk(app, seed):
    teacher = await seed("TEACHER")
    node_id, environments = await setup(app, [teacher.id])
    start = datetime.now(UTC) + timedelta(hours=1)
    reservation = await reserve_lesson(
        app.state.sessions,
        node_id=node_id,
        environment_id=environments[0],
        teacher_id=teacher.id,
        request_id=uuid.uuid4(),
        starts_at=start,
        ends_at=start + timedelta(hours=1),
        demand=ResourceDemand(memory_mib=512, cpu_millicredits=1000, disk_bytes=10 * GIB),
    )
    async with app.state.sessions() as db, db.begin():
        allocation = await db.get(EnvironmentDiskAllocation, environments[0])
        allocation.state = "MATERIALIZED"
    await cancel_future_lesson(
        app.state.sessions, reservation_id=reservation.id, teacher_id=teacher.id
    )
    async with app.state.sessions() as db:
        allocation = await db.get(EnvironmentDiskAllocation, environments[0])
        assert allocation is not None
        assert allocation.state == "MATERIALIZED"
        assert (await db.get(LessonReservation, reservation.id)).state == "CANCELLED"


async def test_adjacent_lessons_use_peak_instead_of_sum(app, seed):
    teachers = [await seed("TEACHER") for _ in range(3)]
    node_id, environments = await setup(app, [teacher.id for teacher in teachers])
    start = datetime.now(UTC) + timedelta(hours=1)

    async def book(index, begins, ends, memory):
        return await reserve_lesson(
            app.state.sessions,
            node_id=node_id,
            environment_id=environments[index],
            teacher_id=teachers[index].id,
            request_id=uuid.uuid4(),
            starts_at=begins,
            ends_at=ends,
            demand=ResourceDemand(memory_mib=memory, cpu_millicredits=1000, disk_bytes=10 * GIB),
        )

    await book(0, start, start + timedelta(hours=1), 1200)
    await book(1, start + timedelta(hours=1), start + timedelta(hours=2), 1200)
    # Both existing lessons overlap this request, but never each other.
    candidate = await book(
        2, start + timedelta(minutes=30), start + timedelta(hours=1, minutes=30), 600
    )
    assert candidate.state == "RESERVED"


async def test_overdue_active_lesson_keeps_compute_until_confirmed_stop(app, seed):
    teachers = [await seed("TEACHER") for _ in range(2)]
    node_id, environments = await setup(app, [teacher.id for teacher in teachers])
    now = datetime.now(UTC)
    async with app.state.sessions() as db, db.begin():
        db.add(
            LessonReservation(
                node_id=node_id,
                environment_id=environments[0],
                teacher_id=teachers[0].id,
                request_id=uuid.uuid4(),
                starts_at=now - timedelta(hours=2),
                ends_at=now - timedelta(hours=1),
                memory_mib=1800,
                cpu_millicredits=1000,
                disk_bytes=10 * GIB,
                hibernation_bytes=0,
                state="ACTIVE",
            )
        )
    with pytest.raises(AdmissionRejected) as error:
        await reserve_lesson(
            app.state.sessions,
            node_id=node_id,
            environment_id=environments[1],
            teacher_id=teachers[1].id,
            request_id=uuid.uuid4(),
            starts_at=now + timedelta(hours=1),
            ends_at=now + timedelta(hours=2),
            demand=ResourceDemand(memory_mib=512, cpu_millicredits=1000, disk_bytes=10 * GIB),
        )
    assert "RAM_INSUFFICIENT" in error.value.reasons
