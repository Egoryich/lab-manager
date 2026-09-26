import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

Role = Literal["STUDENT", "TEACHER", "ADMIN"]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(Input):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    password: SecretStr = Field(min_length=1, max_length=128)

    @field_validator("username")
    @classmethod
    def normalize(cls, value: str):
        return value.lower()


class Register(Login):
    display_name: str = Field(min_length=1, max_length=120)
    password: SecretStr = Field(min_length=12, max_length=128)

    @field_validator("display_name")
    @classmethod
    def clean_name(cls, value: str):
        if not value.strip():
            raise ValueError("Name cannot be blank")
        return value.strip()


class UserView(BaseModel):
    id: uuid.UUID
    username: str
    display_name: str
    roles: list[Role]


class SessionView(BaseModel):
    user: UserView
    csrf_token: str
    can_create_groups: bool


class GroupCreate(Input):
    name: str = Field(min_length=1, max_length=120)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str):
        if not value.strip():
            raise ValueError("Name cannot be blank")
        return value.strip()


class VersionInput(Input):
    expected_version: int = Field(ge=1)


class GroupEdit(VersionInput):
    join_enabled: bool


class GroupJoin(Input):
    code: str = Field(min_length=6, max_length=6, pattern=r"^[a-zA-Z2-9]+$")
    confirm: Literal[True]

    @field_validator("code")
    @classmethod
    def normalize(cls, value: str):
        return value.upper()


class GroupView(BaseModel):
    id: uuid.UUID
    name: str
    version: int
    join_enabled: bool
    join_code: str | None = None
    member_count: int | None = None


class GrantTeacher(Input):
    can_create_groups: bool


class ResetPassword(Input):
    token: SecretStr = Field(min_length=32, max_length=128)
    password: SecretStr = Field(min_length=12, max_length=128)


class ResetGrantView(BaseModel):
    token: str
    expires_in_seconds: int


class ErrorView(BaseModel):
    code: str
    message: str
    request_id: str
