import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from lab_manager.capacity import GIB, MIB, ResourceDemand
from lab_manager.catalog_models import (
    DemoProfileGrant,
    Environment,
    PermissionPolicyRevision,
    PolicyLimit,
    PolicyPermission,
    ProfileVersion,
    TeacherPolicyAssignment,
    TemplateVersion,
)
from lab_manager.lesson_runtime import RunPreparationRejected, prepare_reserved_run
from lab_manager.models import Group, GroupMember, User
from lab_manager.nodes import NodeObservation
from lab_manager.reservation_models import (
    EnvironmentDiskAllocation,
    LessonReservation,
    NodeResourceLedger,
    NodeResourcePolicy,
)
from lab_manager.reservations import AdmissionRejected, cancel_future_lesson, reserve_lesson
from lab_manager.runtime_models import (
    EnvironmentRun,
    ProviderRuntimeBinding,
    RunRuntime,
    Runtime,
)
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.attributes import flag_modified

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


async def ready_roster(app, teacher_id, student_id, environment_id):
    async with app.state.sessions() as db, db.begin():
        environment = await db.get(Environment, environment_id)
        group = await db.get(Group, environment.group_id)
        db.add(GroupMember(group_id=group.id, student_id=student_id))
        group.version += 1
        db.add_all(
            (
                TeacherPolicyAssignment(
                    teacher_id=teacher_id, revision_id=environment.permission_revision_id
                ),
                DemoProfileGrant(
                    revision_id=environment.permission_revision_id,
                    profile_version_id=environment.profile_version_id,
                ),
                PolicyPermission(
                    revision_id=environment.permission_revision_id,
                    key="can_create_lxc",
                    allowed=True,
                ),
                PolicyPermission(
                    revision_id=environment.permission_revision_id,
                    key="can_use_linux_profiles",
                    allowed=True,
                ),
            )
        )
        for key, value in {
            "max_lxc_per_environment": 10,
            "max_vm_per_environment": 0,
            "max_total_ram_mb": 4096,
            "max_cpu_credits": 4,
            "max_disk_gb": 100,
            "max_active_environments": 1,
        }.items():
            db.add(
                PolicyLimit(revision_id=environment.permission_revision_id, key=key, value=value)
            )


async def test_prepared_lesson_reuses_student_and_demo_runtimes_after_stop(app, seed):
    teacher = await seed("TEACHER")
    student = await seed("STUDENT")
    node_id, environments = await setup(app, [teacher.id])
    environment_id = environments[0]
    await ready_roster(app, teacher.id, student.id, environment_id)
    starts_at = datetime.now(UTC) + timedelta(minutes=10)
    demand = ResourceDemand(memory_mib=1024, cpu_millicredits=2000, disk_bytes=20 * GIB)
    reservation = await reserve_lesson(
        app.state.sessions,
        node_id=node_id,
        environment_id=environment_id,
        teacher_id=teacher.id,
        request_id=uuid.uuid4(),
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=1),
        demand=demand,
    )
    attempts = await asyncio.gather(
        *(
            prepare_reserved_run(
                app.state.sessions, reservation_id=reservation.id, teacher_id=teacher.id
            )
            for _ in range(2)
        )
    )
    assert attempts[0].id == attempts[1].id
    run = attempts[0]
    assert run.state == "PREPARING" and run.generation == 1
    repeated = await prepare_reserved_run(
        app.state.sessions, reservation_id=reservation.id, teacher_id=teacher.id
    )
    assert repeated.id == run.id
    async with app.state.sessions() as db:
        first = list(
            await db.scalars(select(Runtime).where(Runtime.environment_id == environment_id))
        )
        assert {item.role for item in first} == {"STUDENT", "DEMO"}
        assert next(item for item in first if item.role == "STUDENT").student_id == student.id
        assert await db.scalar(select(func.count()).select_from(RunRuntime)) == 2
    async with app.state.sessions() as db, db.begin():
        stored_run = await db.get(EnvironmentRun, run.id)
        stored_run.state = "STOPPED"
        stored_run.stopped_at = await db.scalar(select(func.clock_timestamp()))
        stored_reservation = await db.get(LessonReservation, reservation.id)
        stored_reservation.state = "COMPLETED"
        for runtime in await db.scalars(
            select(Runtime).where(Runtime.environment_id == environment_id)
        ):
            runtime.state = "STOPPED"
    next_reservation = await reserve_lesson(
        app.state.sessions,
        node_id=node_id,
        environment_id=environment_id,
        teacher_id=teacher.id,
        request_id=uuid.uuid4(),
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=1),
        demand=demand,
    )
    second = await prepare_reserved_run(
        app.state.sessions, reservation_id=next_reservation.id, teacher_id=teacher.id
    )
    assert second.id != run.id and second.generation == 2
    async with app.state.sessions() as db:
        later = list(
            await db.scalars(select(Runtime).where(Runtime.environment_id == environment_id))
        )
        assert {item.id for item in first} == {item.id for item in later}
        assert await db.scalar(select(func.count()).select_from(RunRuntime)) == 4


async def test_changed_roster_cannot_start_on_old_disk_reservation(app, seed):
    teacher = await seed("TEACHER")
    first_student = await seed("STUDENT")
    second_student = await seed("STUDENT")
    node_id, environments = await setup(app, [teacher.id])
    environment_id = environments[0]
    await ready_roster(app, teacher.id, first_student.id, environment_id)
    starts_at = datetime.now(UTC) + timedelta(minutes=10)
    reservation = await reserve_lesson(
        app.state.sessions,
        node_id=node_id,
        environment_id=environment_id,
        teacher_id=teacher.id,
        request_id=uuid.uuid4(),
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=1),
        demand=ResourceDemand(memory_mib=1024, cpu_millicredits=2000, disk_bytes=20 * GIB),
    )
    async with app.state.sessions() as db, db.begin():
        group_id = await db.scalar(
            select(Environment.group_id).where(Environment.id == environment_id)
        )
        db.add(GroupMember(group_id=group_id, student_id=second_student.id))
        group = await db.get(Group, group_id)
        group.version += 1
    with pytest.raises(RunPreparationRejected, match="BOOKED_DEMAND_CHANGED"):
        await prepare_reserved_run(
            app.state.sessions, reservation_id=reservation.id, teacher_id=teacher.id
        )
    async with app.state.sessions() as db:
        assert (await db.get(LessonReservation, reservation.id)).state == "RESERVED"
        assert await db.scalar(select(func.count()).select_from(Runtime)) == 0


async def test_suspended_teacher_cannot_prepare_booked_lesson(app, seed):
    teacher = await seed("TEACHER")
    student = await seed("STUDENT")
    node_id, environments = await setup(app, [teacher.id])
    environment_id = environments[0]
    await ready_roster(app, teacher.id, student.id, environment_id)
    starts_at = datetime.now(UTC) + timedelta(minutes=10)
    reservation = await reserve_lesson(
        app.state.sessions,
        node_id=node_id,
        environment_id=environment_id,
        teacher_id=teacher.id,
        request_id=uuid.uuid4(),
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=1),
        demand=ResourceDemand(memory_mib=1024, cpu_millicredits=2000, disk_bytes=20 * GIB),
    )
    async with app.state.sessions() as db, db.begin():
        teacher = await db.get(User, teacher.id)
        teacher.status = "SUSPENDED"
    with pytest.raises(RunPreparationRejected, match="TEACHER_NOT_ACTIVE"):
        await prepare_reserved_run(
            app.state.sessions, reservation_id=reservation.id, teacher_id=teacher.id
        )
    async with app.state.sessions() as db:
        assert (await db.get(LessonReservation, reservation.id)).state == "RESERVED"
        assert await db.scalar(select(func.count()).select_from(Runtime)) == 0


async def test_storage_filling_after_booking_blocks_run_preparation(app, seed):
    teacher = await seed("TEACHER")
    student = await seed("STUDENT")
    node_id, environments = await setup(app, [teacher.id])
    environment_id = environments[0]
    await ready_roster(app, teacher.id, student.id, environment_id)
    starts_at = datetime.now(UTC) + timedelta(minutes=10)
    reservation = await reserve_lesson(
        app.state.sessions,
        node_id=node_id,
        environment_id=environment_id,
        teacher_id=teacher.id,
        request_id=uuid.uuid4(),
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=1),
        demand=ResourceDemand(memory_mib=1024, cpu_millicredits=2000, disk_bytes=20 * GIB),
    )
    async with app.state.sessions() as db, db.begin():
        observation = await db.get(NodeObservation, node_id)
        observation.payload["sample"]["storages"][0]["available_bytes"] = 5 * GIB
        flag_modified(observation, "payload")
    with pytest.raises(RunPreparationRejected, match="STORAGE_UNSAFE"):
        await prepare_reserved_run(
            app.state.sessions, reservation_id=reservation.id, teacher_id=teacher.id
        )
    async with app.state.sessions() as db:
        assert (await db.get(LessonReservation, reservation.id)).state == "RESERVED"
        assert await db.scalar(select(func.count()).select_from(Runtime)) == 0


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


async def test_teacher_booking_rechecks_roster_and_keeps_idempotency(
    app, seed, session, client_factory
):
    teacher = await seed("TEACHER")
    student = await seed("STUDENT")
    stranger = await seed("TEACHER")
    node_id, environments = await setup(app, [teacher.id])
    environment_id = environments[0]
    async with app.state.sessions() as db, db.begin():
        environment = await db.get(Environment, environment_id)
        group = await db.get(Group, environment.group_id)
        db.add(GroupMember(group_id=group.id, student_id=student.id))
        group.version += 1
        db.add_all(
            (
                TeacherPolicyAssignment(
                    teacher_id=teacher.id, revision_id=environment.permission_revision_id
                ),
                DemoProfileGrant(
                    revision_id=environment.permission_revision_id,
                    profile_version_id=environment.profile_version_id,
                ),
                PolicyPermission(
                    revision_id=environment.permission_revision_id,
                    key="can_create_lxc",
                    allowed=True,
                ),
                PolicyPermission(
                    revision_id=environment.permission_revision_id,
                    key="can_use_linux_profiles",
                    allowed=True,
                ),
            )
        )
        for key, value in {
            "max_lxc_per_environment": 10,
            "max_vm_per_environment": 0,
            "max_total_ram_mb": 4096,
            "max_cpu_credits": 4,
            "max_disk_gb": 100,
            "max_active_environments": 1,
        }.items():
            db.add(
                PolicyLimit(revision_id=environment.permission_revision_id, key=key, value=value)
            )
        group_id, group_version = group.id, group.version
    starts_at = datetime.now(UTC) + timedelta(hours=1)
    body = {
        "request_id": str(uuid.uuid4()),
        "node_id": str(node_id),
        "expected_environment_version": 1,
        "expected_group_version": group_version,
        "starts_at": starts_at.isoformat(),
        "ends_at": (starts_at + timedelta(hours=1)).isoformat(),
    }
    path = f"/api/environments/{environment_id}/reservations"
    async with client_factory() as owner, client_factory() as other:
        await session(owner, teacher)
        await session(other, stranger)
        assert (await other.post(path, json=body)).status_code == 404
        stale = await owner.post(path, json=body | {"expected_group_version": 1})
        assert stale.status_code == 409 and stale.json()["code"] == "VERSION_CONFLICT"
        booked = await owner.post(path, json=body)
        assert booked.status_code == 201, booked.text
        assert booked.json()["memory_mib"] == 1024
        async with app.state.sessions() as db, db.begin():
            group = await db.get(Group, group_id)
            group.version += 1
        repeat = await owner.post(path, json=body)
        assert repeat.status_code == 201 and repeat.json()["id"] == booked.json()["id"]
        conflict = await owner.post(
            path,
            json=body | {"ends_at": (starts_at + timedelta(hours=2)).isoformat()},
        )
        assert conflict.status_code == 409 and conflict.json()["code"] == "REQUEST_CONFLICT"
        cancelled = await owner.post(f"/api/reservations/{booked.json()['id']}/cancel")
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["state"] == "CANCELLED"
    async with app.state.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(EnvironmentDiskAllocation)) == 0


async def test_lesson_preview_is_scoped_and_never_books(app, seed, session, client_factory):
    teacher = await seed("TEACHER")
    other = await seed("TEACHER")
    node_id, environments = await setup(app, [teacher.id])
    start = datetime.now(UTC) + timedelta(hours=1)
    params = {"starts_at": start.isoformat(), "ends_at": (start + timedelta(hours=2)).isoformat()}
    path = f"/api/environments/{environments[0]}/lesson-preview"
    async with client_factory() as owner, client_factory() as stranger:
        await session(owner, teacher)
        await session(stranger, other)
        assert (await stranger.get(path, params=params)).status_code == 404
        result = await owner.get(path, params=params)
        assert result.status_code == 200, result.text
        preview = result.json()
        assert preview["reservation_created"] is False
        assert preview["student_count"] == 0
        assert preview["nodes"][0]["node_id"] == str(node_id)
        assert preview["nodes"][0]["available"] is False
        assert "GROUP_EMPTY" in preview["nodes"][0]["reasons"]
    async with app.state.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(LessonReservation)) == 0


async def test_same_environment_cannot_be_booked_on_two_nodes(app, seed):
    teacher = await seed("TEACHER")
    first_node, environments = await setup(app, [teacher.id])
    second_node = uuid.uuid4()
    async with app.state.sessions() as db, db.begin():
        source = await db.get(NodeObservation, first_node)
        now = await db.scalar(select(func.clock_timestamp()))
        db.add(
            NodeObservation(
                id=second_node,
                name="Second synthetic node",
                attempt_started_at=now,
                last_contact_at=now,
                sample_finished_at=now,
                payload=source.payload,
            )
        )
        await db.flush()
        db.add(
            NodeResourcePolicy(
                node_id=second_node,
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
        db.add(NodeResourceLedger(node_id=second_node))
    start = datetime.now(UTC) + timedelta(hours=1)

    async def book(node_id):
        return await reserve_lesson(
            app.state.sessions,
            node_id=node_id,
            environment_id=environments[0],
            teacher_id=teacher.id,
            request_id=uuid.uuid4(),
            starts_at=start,
            ends_at=start + timedelta(hours=2),
            demand=ResourceDemand(memory_mib=512, cpu_millicredits=1000, disk_bytes=GIB),
        )

    result = await asyncio.gather(book(first_node), book(second_node), return_exceptions=True)
    assert sum(isinstance(item, LessonReservation) for item in result) == 1
    rejected = next(item for item in result if isinstance(item, AdmissionRejected))
    assert rejected.reasons == ("ENVIRONMENT_ALREADY_BOOKED",)


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


async def test_run_and_runtime_identities_preserve_boundaries(app, seed):
    teachers = [await seed("TEACHER") for _ in range(2)]
    student = await seed("STUDENT")
    node_id, environments = await setup(app, [teacher.id for teacher in teachers])
    begins = datetime.now(UTC) + timedelta(hours=1)
    booking = await reserve_lesson(
        app.state.sessions,
        node_id=node_id,
        environment_id=environments[0],
        teacher_id=teachers[0].id,
        request_id=uuid.uuid4(),
        starts_at=begins,
        ends_at=begins + timedelta(hours=1),
        demand=ResourceDemand(memory_mib=1024, cpu_millicredits=2000, disk_bytes=20 * GIB),
    )
    async with app.state.sessions() as db, db.begin():
        profile_id = (await db.get(Environment, environments[0])).profile_version_id
        run = EnvironmentRun(
            environment_id=environments[0],
            node_id=node_id,
            reservation_id=booking.id,
            generation=1,
        )
        demo = Runtime(
            environment_id=environments[0],
            node_id=node_id,
            role="DEMO",
            student_id=None,
            membership_generation=None,
            profile_version_id=profile_id,
            kind="LXC",
            memory_mib=512,
            vcpu=1,
            disk_gib=10,
        )
        learner = Runtime(
            environment_id=environments[0],
            node_id=node_id,
            role="STUDENT",
            student_id=student.id,
            membership_generation=1,
            profile_version_id=profile_id,
            kind="LXC",
            memory_mib=512,
            vcpu=1,
            disk_gib=10,
        )
        db.add_all((run, demo, learner))
        await db.flush()
        db.add_all(
            (
                RunRuntime(run_id=run.id, runtime_id=demo.id, environment_id=environments[0]),
                RunRuntime(run_id=run.id, runtime_id=learner.id, environment_id=environments[0]),
                ProviderRuntimeBinding(
                    runtime_id=demo.id,
                    node_id=node_id,
                    vmid=510,
                    ownership_marker="lab:" + str(demo.id),
                    generation=1,
                ),
            )
        )
        await db.flush()

        with pytest.raises(IntegrityError):
            async with db.begin_nested():
                db.add(
                    ProviderRuntimeBinding(
                        runtime_id=learner.id,
                        node_id=node_id,
                        vmid=510,
                        ownership_marker="lab:" + str(learner.id),
                        generation=1,
                    )
                )
                await db.flush()

        foreign = Runtime(
            environment_id=environments[1],
            node_id=node_id,
            role="STUDENT",
            student_id=student.id,
            membership_generation=1,
            profile_version_id=profile_id,
            kind="LXC",
            memory_mib=512,
            vcpu=1,
            disk_gib=10,
        )
        db.add(foreign)
        await db.flush()
        with pytest.raises(IntegrityError):
            async with db.begin_nested():
                db.add(
                    RunRuntime(
                        run_id=run.id,
                        runtime_id=foreign.id,
                        environment_id=environments[0],
                    )
                )
                await db.flush()

        assert await db.scalar(select(func.count()).select_from(RunRuntime)) == 2
