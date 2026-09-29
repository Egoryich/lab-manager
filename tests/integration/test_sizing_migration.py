"""An installed 0004 catalog keeps its draft sizes through the 0005 migration."""

import asyncio
import os
import re
import uuid

import pytest
from alembic import command
from alembic.config import Config
from lab_manager.config import Settings
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.integration


async def test_existing_profiles_and_drafts_become_fixed_sized():
    if os.getenv("LAB_RUN_INTEGRATION") != "1":
        pytest.skip("Set LAB_RUN_INTEGRATION=1 for isolated PostgreSQL migration tests")
    base = Settings()
    if base.environment not in {"development", "test"}:
        pytest.fail("Migration test refuses production settings")
    schema = "lab_test_" + uuid.uuid4().hex
    admin = create_async_engine(base.database_url.get_secret_value())
    async with admin.begin() as db:
        await db.execute(text(f'CREATE SCHEMA "{schema}"'))
    url = (
        make_url(base.database_url.get_secret_value())
        .update_query_dict({"options": f"-csearch_path={schema}"})
        .render_as_string(hide_password=False)
    )
    config = Config("alembic.ini")
    config.attributes["database_url"] = url
    engine = create_async_engine(url)
    user, group, template, profile, demo, policy, environment = [uuid.uuid4() for _ in range(7)]
    try:
        await asyncio.to_thread(command.upgrade, config, "0004_nodes")
        async with engine.begin() as db:
            await db.execute(
                text(
                    "INSERT INTO users (id, username, display_name, status, auth_revision) "
                    "VALUES (:id, 'teacher', 'Teacher', 'ACTIVE', 1)"
                ),
                {"id": user},
            )
            await db.execute(
                text(
                    "INSERT INTO groups (id, name, owner_teacher_id, join_enabled, version) "
                    "VALUES (:id, 'Group', :owner, true, 1)"
                ),
                {"id": group, "owner": user},
            )
            await db.execute(
                text(
                    "INSERT INTO template_versions "
                    "(id, name, version_label, runtime_kind, guest_family, created_by) "
                    "VALUES (:id, 'Linux', 'v1', 'LXC', 'LINUX', :owner)"
                ),
                {"id": template, "owner": user},
            )
            for identity, ram, disk in ((profile, 512, 10), (demo, 2048, 20)):
                await db.execute(
                    text(
                        "INSERT INTO profile_versions "
                        "(id, name, template_version_id, memory_mib, vcpu, "
                        "cpu_millicredits, disk_gib, network_mode, internet_enabled, created_by) "
                        "VALUES (:id, 'Profile', :template, :ram, 1, 1000, :disk, "
                        "'ISOLATED', false, :owner)"
                    ),
                    {"id": identity, "template": template, "ram": ram, "disk": disk, "owner": user},
                )
            await db.execute(
                text(
                    "INSERT INTO permission_policy_revisions (id, name, created_by) "
                    "VALUES (:id, 'Policy', :owner)"
                ),
                {"id": policy, "owner": user},
            )
            await db.execute(
                text(
                    "INSERT INTO environments "
                    "(id, name, group_id, owner_teacher_id, profile_version_id, "
                    "demo_profile_version_id, permission_revision_id, request_id, version) "
                    "VALUES (:id, 'Lesson', :group, :owner, :profile, :demo, :policy, "
                    ":request, 1)"
                ),
                {
                    "id": environment,
                    "group": group,
                    "owner": user,
                    "profile": profile,
                    "demo": demo,
                    "policy": policy,
                    "request": uuid.uuid4(),
                },
            )
        await asyncio.to_thread(command.upgrade, config, "head")
        async with engine.connect() as db:
            fixed = (
                await db.execute(
                    text(
                        "SELECT min_memory_mib, max_memory_mib, min_vcpu, max_vcpu, "
                        "min_disk_gib, max_disk_gib FROM profile_versions WHERE id=:id"
                    ),
                    {"id": profile},
                )
            ).one()
            assert tuple(fixed) == (512, 512, 1, 1, 10, 10)
            selected = (
                await db.execute(
                    text(
                        "SELECT student_memory_mib, student_vcpu, student_disk_gib, "
                        "demo_memory_mib, demo_vcpu, demo_disk_gib "
                        "FROM environments WHERE id=:id"
                    ),
                    {"id": environment},
                )
            ).one()
            assert tuple(selected) == (512, 1, 10, 2048, 1, 20)
    finally:
        await engine.dispose()
        assert re.fullmatch(r"lab_test_[a-f0-9]{32}", schema)
        async with admin.begin() as db:
            await db.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()
