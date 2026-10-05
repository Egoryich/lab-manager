import uuid
from types import SimpleNamespace

from lab_manager.runtime_access import may_open


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
