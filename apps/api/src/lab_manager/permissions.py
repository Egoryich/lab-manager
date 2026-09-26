from typing import get_args

from sqlalchemy import select

from lab_manager.catalog_models import (
    DemoProfileGrant,
    PolicyLimit,
    PolicyPermission,
    TeacherPolicyAssignment,
)
from lab_manager.catalog_schemas import EffectivePolicy, LimitKey, PermissionKey
from lab_manager.models import TeacherPermissionOverride


async def effective_policy(db, teacher_id):
    permissions = dict.fromkeys(get_args(PermissionKey), False)
    limits = dict.fromkeys(get_args(LimitKey), 0)
    assignment = await db.get(TeacherPolicyAssignment, teacher_id)
    demos = []
    if assignment:
        rows = await db.scalars(
            select(PolicyPermission).where(PolicyPermission.revision_id == assignment.revision_id)
        )
        permissions.update({row.key: row.allowed for row in rows})
        rows = await db.scalars(
            select(PolicyLimit).where(PolicyLimit.revision_id == assignment.revision_id)
        )
        limits.update({row.key: row.value for row in rows})
        demos = list(
            await db.scalars(
                select(DemoProfileGrant.profile_version_id).where(
                    DemoProfileGrant.revision_id == assignment.revision_id
                )
            )
        )
    overrides = await db.scalars(
        select(TeacherPermissionOverride).where(TeacherPermissionOverride.teacher_id == teacher_id)
    )
    permissions.update({row.key: row.allowed for row in overrides})
    permissions["can_create_snapshots"] = permissions["can_restore_snapshots"] = False
    return EffectivePolicy(
        revision_id=assignment.revision_id if assignment else None,
        assignment_version=assignment.version if assignment else 0,
        permissions=permissions,
        limits=limits,
        demo_profile_ids=demos,
    )


def profile_allowed(profile, template, policy, *, demo=False):
    if demo:
        # Explicit demo grant does not grant the corresponding student runtime type.
        granted = profile.id in policy.demo_profile_ids
    else:
        kind = "can_create_lxc" if template.runtime_kind == "LXC" else "can_create_qemu"
        family = (
            "can_use_linux_profiles"
            if template.guest_family == "LINUX"
            else "can_use_windows_profiles"
        )
        granted = policy.permissions[kind] and policy.permissions[family]
    return (
        granted
        and (not profile.internet_enabled or policy.permissions["can_allow_internet_access"])
        and (profile.network_mode != "GROUP_LAN" or policy.permissions["can_enable_group_network"])
    )
