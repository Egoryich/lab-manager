import uuid
from typing import get_args

from fastapi import APIRouter, Query, Request
from sqlalchemy import and_, func, or_, select

from lab_manager.audit import audit
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
from lab_manager.catalog_schemas import (
    EffectivePolicy,
    EnvironmentCreate,
    EnvironmentView,
    EstimateView,
    LimitKey,
    MachineSizing,
    PermissionKey,
    PolicyAssign,
    PolicyCreate,
    PolicyView,
    ProfileCreate,
    ProfileView,
    ResourceTotal,
    TemplateCreate,
    TemplateView,
)
from lab_manager.dependencies import DB, Actor, Problem, require_role
from lab_manager.models import Group, GroupMember, User, UserRole
from lab_manager.permissions import effective_policy, profile_allowed

router = APIRouter(tags=["Catalog and environments"])


@router.post("/admin/template-versions", response_model=TemplateView, status_code=201)
async def template_create(body: TemplateCreate, actor: Actor, db: DB, request: Request):
    require_role(actor, "ADMIN")
    template = TemplateVersion(**body.model_dump(), created_by=actor.id)
    db.add(template)
    await db.flush()
    audit(db, request, actor.id, "template_version.registered", template.id)
    await db.commit()
    return template


@router.get("/admin/template-versions", response_model=list[TemplateView])
async def templates(
    actor: Actor, db: DB, after: uuid.UUID | None = None, limit: int = Query(100, ge=1, le=100)
):
    require_role(actor, "ADMIN")
    query = select(TemplateVersion).order_by(TemplateVersion.id).limit(limit)
    if after:
        query = query.where(TemplateVersion.id > after)
    return list(await db.scalars(query))


@router.post("/admin/profile-versions", response_model=ProfileView, status_code=201)
async def profile_create(body: ProfileCreate, actor: Actor, db: DB, request: Request):
    require_role(actor, "ADMIN")
    template = await db.get(TemplateVersion, body.template_version_id)
    if not template:
        raise Problem(404, "NOT_FOUND", "Версия шаблона не найдена.")
    profile = ProfileVersion(**body.model_dump(), created_by=actor.id)
    db.add(profile)
    await db.flush()
    audit(db, request, actor.id, "profile_version.created", profile.id)
    await db.commit()
    return profile_view(profile, template)


def profile_view(profile, template, student=False, demo=False):
    return ProfileView(
        **{key: getattr(profile, key) for key in ProfileCreate.model_fields},
        id=profile.id,
        runtime_kind=template.runtime_kind,
        guest_family=template.guest_family,
        student_allowed=student,
        demo_allowed=demo,
    )


@router.get("/profiles", response_model=list[ProfileView])
async def profiles(
    actor: Actor, db: DB, after: uuid.UUID | None = None, limit: int = Query(100, ge=1, le=100)
):
    if not {"TEACHER", "ADMIN"}.intersection(actor.roles):
        raise Problem(403, "FORBIDDEN", "Недостаточно прав.")
    policy = await effective_policy(db, actor.id)
    query = (
        select(ProfileVersion, TemplateVersion).join(TemplateVersion).order_by(ProfileVersion.id)
    )
    if after:
        query = query.where(ProfileVersion.id > after)
    if "ADMIN" not in actor.roles:
        kinds = [
            k
            for k, perm in [("LXC", "can_create_lxc"), ("QEMU", "can_create_qemu")]
            if policy.permissions[perm]
        ]
        families = [
            k
            for k, perm in [
                ("LINUX", "can_use_linux_profiles"),
                ("WINDOWS", "can_use_windows_profiles"),
            ]
            if policy.permissions[perm]
        ]
        query = query.where(
            or_(
                and_(
                    TemplateVersion.runtime_kind.in_(kinds),
                    TemplateVersion.guest_family.in_(families),
                ),
                ProfileVersion.id.in_(policy.demo_profile_ids),
            )
        )
        if not policy.permissions["can_allow_internet_access"]:
            query = query.where(ProfileVersion.internet_enabled.is_(False))
        if not policy.permissions["can_enable_group_network"]:
            query = query.where(ProfileVersion.network_mode == "ISOLATED")
    result = []
    for profile, template in (await db.execute(query.limit(limit))).all():
        student = profile_allowed(profile, template, policy)
        demo = profile_allowed(profile, template, policy, demo=True)
        if "ADMIN" in actor.roles or student or demo:
            result.append(profile_view(profile, template, student, demo))
            if len(result) == limit:
                break
    return result


@router.post("/admin/permission-policies", response_model=PolicyView, status_code=201)
async def policy_create(body: PolicyCreate, actor: Actor, db: DB, request: Request):
    require_role(actor, "ADMIN")
    demos = set(body.demo_profile_ids)
    found = set(await db.scalars(select(ProfileVersion.id).where(ProfileVersion.id.in_(demos))))
    if found != demos:
        raise Problem(404, "NOT_FOUND", "Демонстрационный профиль не найден.")
    revision = PermissionPolicyRevision(name=body.name, created_by=actor.id)
    db.add(revision)
    await db.flush()
    permissions = dict.fromkeys(get_args(PermissionKey), False) | body.permissions
    limits = dict.fromkeys(get_args(LimitKey), 0) | body.limits
    db.add_all(
        [
            PolicyPermission(revision_id=revision.id, key=k, allowed=v)
            for k, v in permissions.items()
        ]
    )
    db.add_all([PolicyLimit(revision_id=revision.id, key=k, value=v) for k, v in limits.items()])
    db.add_all([DemoProfileGrant(revision_id=revision.id, profile_version_id=p) for p in demos])
    audit(db, request, actor.id, "permission_policy.created", revision.id)
    await db.commit()
    return PolicyView(
        id=revision.id,
        name=revision.name,
        permissions=permissions,
        limits=limits,
        demo_profile_ids=list(demos),
    )


@router.get("/admin/permission-policies", response_model=list[PolicyView])
async def policies(
    actor: Actor, db: DB, after: uuid.UUID | None = None, limit: int = Query(100, ge=1, le=100)
):
    require_role(actor, "ADMIN")
    query = select(PermissionPolicyRevision).order_by(PermissionPolicyRevision.id).limit(limit)
    if after:
        query = query.where(PermissionPolicyRevision.id > after)
    result = []
    for revision in await db.scalars(query):
        permissions = {
            r.key: r.allowed
            for r in await db.scalars(
                select(PolicyPermission).where(PolicyPermission.revision_id == revision.id)
            )
        }
        limits = {
            r.key: r.value
            for r in await db.scalars(
                select(PolicyLimit).where(PolicyLimit.revision_id == revision.id)
            )
        }
        demos = list(
            await db.scalars(
                select(DemoProfileGrant.profile_version_id).where(
                    DemoProfileGrant.revision_id == revision.id
                )
            )
        )
        result.append(
            PolicyView(
                id=revision.id,
                name=revision.name,
                permissions=permissions,
                limits=limits,
                demo_profile_ids=demos,
            )
        )
    return result


@router.get("/teachers/{teacher_id}/permissions", response_model=EffectivePolicy)
async def teacher_permissions(teacher_id: uuid.UUID, actor: Actor, db: DB):
    if "ADMIN" not in actor.roles and teacher_id != actor.id:
        raise Problem(404, "NOT_FOUND", "Преподаватель не найден.")
    if not await db.get(UserRole, (teacher_id, "TEACHER")):
        raise Problem(404, "NOT_FOUND", "Преподаватель не найден.")
    return await effective_policy(db, teacher_id)


@router.put("/admin/teachers/{teacher_id}/policy", response_model=EffectivePolicy)
async def assign_policy(
    teacher_id: uuid.UUID, body: PolicyAssign, actor: Actor, db: DB, request: Request
):
    require_role(actor, "ADMIN")
    teacher = await db.scalar(select(User).where(User.id == teacher_id).with_for_update())
    if (
        not teacher
        or teacher.status != "ACTIVE"
        or not await db.get(UserRole, (teacher_id, "TEACHER"))
    ):
        raise Problem(404, "NOT_FOUND", "Преподаватель не найден.")
    if not await db.get(PermissionPolicyRevision, body.revision_id):
        raise Problem(404, "NOT_FOUND", "Политика не найдена.")
    assignment = await db.get(TeacherPolicyAssignment, teacher_id)
    if body.expected_version != (assignment.version if assignment else 0):
        raise Problem(409, "VERSION_CONFLICT", "Назначение изменилось. Обновите страницу.")
    if assignment:
        assignment.revision_id = body.revision_id
        assignment.version += 1
    else:
        db.add(TeacherPolicyAssignment(teacher_id=teacher_id, revision_id=body.revision_id))
    audit(
        db,
        request,
        actor.id,
        "teacher.policy_assigned",
        teacher_id,
        revision_id=str(body.revision_id),
    )
    await db.commit()
    return await effective_policy(db, teacher_id)


def selected_resources(profile, requested):
    choice = requested or MachineSizing(
        memory_mib=profile.memory_mib, vcpu=profile.vcpu, disk_gib=profile.disk_gib
    )
    if any(
        not getattr(profile, f"min_{field}")
        <= getattr(choice, field)
        <= getattr(profile, f"max_{field}")
        for field in MachineSizing.model_fields
    ):
        raise Problem(422, "PROFILE_RESOURCE_RANGE", "Размеры машины вне границ профиля.")
    return choice


def resources(profile, template, count, choice):
    return ResourceTotal(
        machines=count,
        memory_mib=choice.memory_mib * count,
        vcpu=choice.vcpu * count,
        cpu_millicredits=(profile.cpu_millicredits * choice.vcpu + profile.vcpu - 1)
        // profile.vcpu
        * count,
        disk_bytes=choice.disk_gib * 2**30 * count,
        hibernation_bytes=choice.memory_mib * 2**20 * count
        if template.runtime_kind == "QEMU"
        else 0,
    )


async def estimate(
    db, group, profile, template, demo_profile, demo_template, policy, student_choice, demo_choice
):
    count = await db.scalar(
        select(func.count())
        .select_from(GroupMember)
        .where(GroupMember.group_id == group.id, GroupMember.status == "ACTIVE")
    )
    students = resources(profile, template, count, student_choice)
    demo = resources(demo_profile, demo_template, 1, demo_choice)
    total = ResourceTotal(
        **{key: getattr(students, key) + getattr(demo, key) for key in ResourceTotal.model_fields}
    )
    violations = []
    kind_limit = (
        "max_lxc_per_environment" if template.runtime_kind == "LXC" else "max_vm_per_environment"
    )
    checks = [
        (kind_limit, count),
        ("max_total_ram_mb", total.memory_mib),
        ("max_cpu_credits", (total.cpu_millicredits + 999) // 1000),
        ("max_disk_gb", (total.disk_bytes + total.hibernation_bytes + 2**30 - 1) // 2**30),
    ]
    for key, required in checks:
        if required > policy.limits[key]:
            violations.append(key)
    return EstimateView(
        student_count=count,
        group_version=group.version,
        students=students,
        demo=demo,
        total=total,
        within_per_environment_limits=not violations,
        violations=violations,
    )


async def configuration(db, actor, body, *, lock=False):
    require_role(actor, "TEACHER")
    if lock:
        await db.scalar(select(User).where(User.id == actor.id).with_for_update())
    query = select(Group).where(Group.id == body.group_id, Group.owner_teacher_id == actor.id)
    if lock:
        query = query.with_for_update()
    group = await db.scalar(query)
    if not group:
        raise Problem(404, "NOT_FOUND", "Группа не найдена.")
    if group.archived_at:
        raise Problem(409, "GROUP_ARCHIVED", "Группа закрыта.")
    policy = await effective_policy(db, actor.id)
    selected = []
    for profile_id, is_demo in [
        (body.profile_version_id, False),
        (body.demo_profile_version_id, True),
    ]:
        profile = await db.get(ProfileVersion, profile_id)
        template = await db.get(TemplateVersion, profile.template_version_id) if profile else None
        if (
            not profile
            or not policy.revision_id
            or not profile_allowed(profile, template, policy, demo=is_demo)
        ):
            raise Problem(
                403, "PROFILE_FORBIDDEN", "Профиль или его сетевые настройки не разрешены."
            )
        selected.extend([profile, template])
    student_choice = selected_resources(selected[0], body.student_resources)
    demo_choice = selected_resources(selected[2], body.demo_resources)
    return group, policy, selected, student_choice, demo_choice


@router.post("/environments/estimate", response_model=EstimateView)
async def estimate_environment(body: EnvironmentCreate, actor: Actor, db: DB):
    group, policy, selected, student_choice, demo_choice = await configuration(db, actor, body)
    return await estimate(db, group, *selected, policy, student_choice, demo_choice)


@router.post("/environments", response_model=EnvironmentView, status_code=201)
async def create_environment(body: EnvironmentCreate, actor: Actor, db: DB, request: Request):
    group, policy, selected, student_choice, demo_choice = await configuration(
        db, actor, body, lock=True
    )
    values = {
        "name": body.name,
        "group_id": body.group_id,
        "profile_version_id": body.profile_version_id,
        "demo_profile_version_id": body.demo_profile_version_id,
        "request_id": body.request_id,
        **{f"student_{key}": getattr(student_choice, key) for key in MachineSizing.model_fields},
        **{f"demo_{key}": getattr(demo_choice, key) for key in MachineSizing.model_fields},
    }
    previous = await db.scalar(
        select(Environment).where(
            Environment.owner_teacher_id == actor.id, Environment.request_id == body.request_id
        )
    )
    if previous:
        if any(getattr(previous, key) != value for key, value in values.items()):
            raise Problem(
                409, "IDEMPOTENCY_CONFLICT", "Этот запрос уже использован для другой конфигурации."
            )
        return previous
    result = await estimate(db, group, *selected, policy, student_choice, demo_choice)
    if not result.within_per_environment_limits:
        raise Problem(
            409,
            "QUOTA_EXCEEDED",
            "Окружение превышает разрешённые лимиты: " + ", ".join(result.violations),
        )
    environment = Environment(
        **values, owner_teacher_id=actor.id, permission_revision_id=policy.revision_id
    )
    db.add(environment)
    await db.flush()
    audit(db, request, actor.id, "environment.draft_created", environment.id)
    await db.commit()
    return environment


@router.get("/environments", response_model=list[EnvironmentView])
async def environments(
    actor: Actor,
    db: DB,
    group_id: uuid.UUID | None = None,
    after: uuid.UUID | None = None,
    limit: int = Query(50, ge=1, le=100),
):
    query = select(Environment).order_by(Environment.id).limit(limit)
    if "ADMIN" not in actor.roles:
        # Drafts are preparation work, not yet a lesson visible to students.
        require_role(actor, "TEACHER")
        query = query.where(Environment.owner_teacher_id == actor.id)
    if group_id:
        query = query.where(Environment.group_id == group_id)
    if after:
        query = query.where(Environment.id > after)
    return list(await db.scalars(query))
