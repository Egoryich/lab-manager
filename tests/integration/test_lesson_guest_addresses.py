import pytest
from lab_manager.catalog_models import Environment, ProfileVersion
from lab_manager.lesson_network import claim_run_networks
from lab_manager.runtime_models import Runtime

from tests.integration.test_network_allocations import setup

pytestmark = pytest.mark.integration


async def test_guest_address_is_stable_across_lesson_preparation(app, seed):
    teacher = await seed("TEACHER")
    node_id, environment_id = await setup(app, teacher.id)
    async with app.state.sessions() as db, db.begin():
        environment = await db.get(Environment, environment_id)
        profile = await db.get(ProfileVersion, environment.profile_version_id)
        runtime = Runtime(
            environment_id=environment_id,
            node_id=node_id,
            role="DEMO",
            student_id=None,
            membership_generation=None,
            profile_version_id=profile.id,
            kind="LXC",
            memory_mib=512,
            vcpu=1,
            disk_gib=10,
            generation=1,
            state="PLANNED",
        )
        db.add(runtime)
        await db.flush()
        first = await claim_run_networks(
            db,
            environment_id=environment_id,
            node_id=node_id,
            runtimes=[runtime],
            profiles={profile.id: profile},
        )
        assert first[runtime.id] == runtime.network_allocation_id
        assert runtime.guest_ipv4 == "10.70.0.2"
        identity = (runtime.network_allocation_id, runtime.guest_ipv4)
    async with app.state.sessions() as db, db.begin():
        runtime = await db.get(Runtime, runtime.id)
        second = await claim_run_networks(
            db,
            environment_id=environment_id,
            node_id=node_id,
            runtimes=[runtime],
            profiles={profile.id: profile},
        )
        assert second[runtime.id] == identity[0]
        assert (runtime.network_allocation_id, runtime.guest_ipv4) == identity


async def test_separate_isolated_guests_get_separate_subnets(app, seed):
    teacher = await seed("TEACHER")
    student = await seed("STUDENT")
    node_id, environment_id = await setup(app, teacher.id)
    async with app.state.sessions() as db, db.begin():
        environment = await db.get(Environment, environment_id)
        profile = await db.get(ProfileVersion, environment.profile_version_id)
        demo = Runtime(
            environment_id=environment_id,
            node_id=node_id,
            role="DEMO",
            student_id=None,
            membership_generation=None,
            profile_version_id=profile.id,
            kind="LXC",
            memory_mib=512,
            vcpu=1,
            disk_gib=10,
            generation=1,
            state="PLANNED",
        )
        guest = Runtime(
            environment_id=environment_id,
            node_id=node_id,
            role="STUDENT",
            student_id=student.id,
            membership_generation=1,
            profile_version_id=profile.id,
            kind="LXC",
            memory_mib=512,
            vcpu=1,
            disk_gib=10,
            generation=1,
            state="PLANNED",
        )
        db.add_all((demo, guest))
        await db.flush()
        await claim_run_networks(
            db,
            environment_id=environment_id,
            node_id=node_id,
            runtimes=[guest, demo],
            profiles={profile.id: profile},
        )
        assert demo.network_allocation_id != guest.network_allocation_id
        assert {demo.guest_ipv4, guest.guest_ipv4} == {"10.70.0.2", "10.70.0.6"}
