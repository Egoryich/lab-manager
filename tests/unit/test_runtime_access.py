import uuid
from types import SimpleNamespace

from cryptography.fernet import Fernet
from fastapi import Response
from lab_manager.config import Settings
from lab_manager.runtime_access import launch_response, may_open


def test_student_only_opens_current_own_machine_and_teacher_can_assist():
    teacher_id, student_id, other_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    environment = SimpleNamespace(owner_teacher_id=teacher_id)
    runtime = SimpleNamespace(role="STUDENT", student_id=student_id, membership_generation=2)
    member = SimpleNamespace(status="ACTIVE", generation=2)
    student = SimpleNamespace(id=student_id, roles=["STUDENT"])
    assert may_open(student, runtime, environment, member)
    assert may_open(SimpleNamespace(id=teacher_id, roles=["TEACHER"]), runtime, environment, None)
    assert not may_open(SimpleNamespace(id=other_id, roles=["TEACHER"]), runtime, environment, None)
    assert not may_open(
        SimpleNamespace(id=other_id, roles=["STUDENT"]), runtime, environment, member
    )
    member.status = "REMOVED"
    assert not may_open(student, runtime, environment, member)
    member.status, member.generation = "ACTIVE", 3
    assert not may_open(student, runtime, environment, member)
    runtime.role = "DEMO"
    assert not may_open(student, runtime, environment, member)


def test_blank_optional_guacamole_secret_keeps_api_available():
    settings = Settings(
        _env_file=None,
        environment="test",
        database_url="postgresql+psycopg://lab:pass@localhost/lab",
        redis_url="redis://localhost:6379/0",
        public_origin="http://localhost:5173",
        encryption_key=Fernet.generate_key().decode(),
        digest_key="d" * 64,
        guacamole_json_secret="",
    )
    assert settings.guacamole_json_secret is None


def test_browser_launch_nonce_stays_in_scoped_httponly_cookie():
    nonce = "one-time-secret-value"
    response = Response()
    result = launch_response(response, "production", nonce)
    cookie = response.headers["set-cookie"]

    assert result.model_dump() == {"url": "/guacamole/"}
    assert nonce not in result.model_dump_json()
    assert cookie.startswith("__Secure-lab_guac_launch=" + nonce + ";")
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=strict" in cookie
    assert "Path=/guacamole" in cookie
    assert "Max-Age=45" in cookie
    assert response.headers["Cache-Control"] == "no-store"
