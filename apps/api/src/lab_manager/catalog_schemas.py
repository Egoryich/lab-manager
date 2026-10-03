import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from lab_manager.schemas import GroupCreate, Input

PermissionKey = Literal[
    "can_create_groups",
    "can_delete_groups",
    "can_create_lxc",
    "can_create_qemu",
    "can_use_linux_profiles",
    "can_use_windows_profiles",
    "can_use_custom_profiles",
    "can_create_demo_vm",
    "can_enable_group_network",
    "can_change_network_policy",
    "can_allow_internet_access",
    "can_create_snapshots",
    "can_restore_snapshots",
    "can_delete_own_environments",
    "can_archive_environments",
    "can_override_idle_policy",
    "can_override_resource_limits",
    "can_power_on_node",
    "can_request_node_shutdown",
    "can_use_exclusive_mode",
]
LimitKey = Literal[
    "max_lxc_per_environment",
    "max_vm_per_environment",
    "max_total_ram_mb",
    "max_cpu_credits",
    "max_disk_gb",
    "max_active_environments",
]


class TemplateCreate(GroupCreate):
    version_label: str = Field(min_length=1, max_length=64)
    runtime_kind: Literal["LXC", "QEMU"]
    guest_family: Literal["LINUX", "WINDOWS"]

    @model_validator(mode="after")
    def compatible(self):
        if self.runtime_kind == "LXC" and self.guest_family != "LINUX":
            raise ValueError("Windows requires QEMU")
        return self


class TemplateView(TemplateCreate):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    deployment_status: Literal["NOT_VERIFIED"] = "NOT_VERIFIED"


class ProfileCreate(GroupCreate):
    template_version_id: uuid.UUID
    memory_mib: int = Field(ge=128, le=1048576)
    vcpu: int = Field(ge=1, le=128)
    cpu_millicredits: int | None = Field(default=None, ge=1, le=128000)
    disk_gib: int = Field(ge=1, le=1048576)
    min_memory_mib: int | None = Field(default=None, ge=128, le=1048576)
    max_memory_mib: int | None = Field(default=None, ge=128, le=1048576)
    min_vcpu: int | None = Field(default=None, ge=1, le=128)
    max_vcpu: int | None = Field(default=None, ge=1, le=128)
    min_disk_gib: int | None = Field(default=None, ge=1, le=1048576)
    max_disk_gib: int | None = Field(default=None, ge=1, le=1048576)
    network_mode: Literal["ISOLATED", "GROUP_LAN"]
    internet_enabled: bool

    @model_validator(mode="after")
    def resource_range(self):
        if self.cpu_millicredits is None:
            self.cpu_millicredits = self.vcpu * 1000
        for field in ("memory_mib", "vcpu", "disk_gib"):
            preferred = getattr(self, field)
            minimum = getattr(self, f"min_{field}")
            maximum = getattr(self, f"max_{field}")
            minimum = preferred if minimum is None else minimum
            maximum = preferred if maximum is None else maximum
            if not minimum <= preferred <= maximum:
                raise ValueError(f"{field} must be within its allowed range")
            setattr(self, f"min_{field}", minimum)
            setattr(self, f"max_{field}", maximum)
        return self


class ProfileView(ProfileCreate):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    cpu_millicredits: int
    min_memory_mib: int
    max_memory_mib: int
    min_vcpu: int
    max_vcpu: int
    min_disk_gib: int
    max_disk_gib: int
    runtime_kind: Literal["LXC", "QEMU"]
    guest_family: Literal["LINUX", "WINDOWS"]
    student_allowed: bool = False
    demo_allowed: bool = False


class PolicyCreate(GroupCreate):
    permissions: dict[PermissionKey, bool]
    limits: dict[LimitKey, StrictInt]
    demo_profile_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def limits_nonnegative(self):
        if any(type(value) is not int or not 0 <= value <= 10**9 for value in self.limits.values()):
            raise ValueError("Limits must be nonnegative integers")
        if self.permissions.get("can_create_snapshots") or self.permissions.get(
            "can_restore_snapshots"
        ):
            raise ValueError("Snapshots are administrator-only")
        return self


class PolicyView(PolicyCreate):
    id: uuid.UUID


class PolicyAssign(Input):
    revision_id: uuid.UUID
    expected_version: int = Field(ge=0)


class EffectivePolicy(BaseModel):
    revision_id: uuid.UUID | None
    assignment_version: int
    permissions: dict[PermissionKey, bool]
    limits: dict[LimitKey, int]
    demo_profile_ids: list[uuid.UUID]


class MachineSizing(Input):
    memory_mib: int = Field(ge=128, le=1048576)
    vcpu: int = Field(ge=1, le=128)
    disk_gib: int = Field(ge=1, le=1048576)


class EnvironmentCreate(GroupCreate):
    group_id: uuid.UUID
    profile_version_id: uuid.UUID
    demo_profile_version_id: uuid.UUID
    request_id: uuid.UUID
    student_resources: MachineSizing | None = None
    demo_resources: MachineSizing | None = None


class ResourceTotal(BaseModel):
    machines: int
    memory_mib: int
    vcpu: int
    cpu_millicredits: int
    disk_bytes: int
    hibernation_bytes: int


class EstimateView(BaseModel):
    student_count: int
    group_version: int
    students: ResourceTotal
    demo: ResourceTotal
    total: ResourceTotal
    within_per_environment_limits: bool
    violations: list[str]
    reservation_created: Literal[False] = False
    admission_status: Literal["NOT_CHECKED"] = "NOT_CHECKED"


class EnvironmentView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    group_id: uuid.UUID
    profile_version_id: uuid.UUID
    demo_profile_version_id: uuid.UUID
    student_memory_mib: int
    student_vcpu: int
    student_disk_gib: int
    demo_memory_mib: int
    demo_vcpu: int
    demo_disk_gib: int
    version: int
    state: Literal["DRAFT"] = "DRAFT"
