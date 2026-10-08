import hashlib
import uuid

import pytest

from lab_node_agent.command_journal import CommandJournal, JournalError


def identity():
    return (
        uuid.uuid4(),
        hashlib.sha256(b"canonical command").hexdigest(),
        "LXC_CREATE",
        901001,
        uuid.uuid4(),
        1,
    )


def test_intent_is_durable_and_duplicate_cannot_repeat_effect(tmp_path):
    path = tmp_path / "receipts.sqlite3"
    values = identity()
    journal = CommandJournal(path)
    receipt, created = journal.begin(*values)
    assert created and receipt.state == "INTENT"
    journal.close()

    journal = CommandJournal(path)
    replay, created = journal.begin(*values)
    assert not created and replay == receipt
    submitted = journal.transition(
        values[0], from_state="INTENT", to_state="SUBMITTED", task_id="UPID"
    )
    assert submitted.task_id == "UPID"
    journal.close()

    journal = CommandJournal(path)
    replay, created = journal.begin(*values)
    assert not created and replay.state == "SUBMITTED"
    done = journal.transition(values[0], from_state="SUBMITTED", to_state="SUCCEEDED")
    assert done.task_id == "UPID"
    with pytest.raises(JournalError, match="RECEIPT_STATE_CONFLICT"):
        journal.transition(values[0], from_state="SUBMITTED", to_state="SUCCEEDED")
    journal.close()


def test_same_operation_id_with_changed_request_rejected(tmp_path):
    journal = CommandJournal(tmp_path / "receipts.sqlite3")
    values = identity()
    journal.begin(*values)
    with pytest.raises(JournalError, match="OPERATION_ID_CONFLICT"):
        journal.begin(values[0], "f" * 64, *values[2:])
    with pytest.raises(JournalError, match="OPERATION_ID_CONFLICT"):
        journal.begin(values[0], values[1], values[2], values[3] + 1, *values[4:])
    journal.close()


def test_different_operation_for_same_vmid_waits_for_reconciliation(tmp_path):
    journal = CommandJournal(tmp_path / "receipts.sqlite3")
    first = identity()
    second = (uuid.uuid4(), first[1], "LXC_START", first[3], first[4], first[5])
    journal.begin(*first)
    with pytest.raises(JournalError, match="VMID_OPERATION_IN_PROGRESS"):
        journal.begin(*second)
    journal.transition(first[0], from_state="INTENT", to_state="SUBMITTED", task_id="UPID")
    with pytest.raises(JournalError, match="VMID_OPERATION_IN_PROGRESS"):
        journal.begin(*second)
    journal.transition(first[0], from_state="SUBMITTED", to_state="SUCCEEDED")
    assert journal.begin(*second)[1] is True
    journal.close()


def test_expected_configuration_is_durable_and_bound_to_operation(tmp_path):
    path = tmp_path / "receipts.sqlite3"
    journal = CommandJournal(path)
    values = identity()
    expected = {"storage": "student-lvm", "bridge": "lmbrabc123"}
    journal.begin(*values, expected)
    journal.close()

    journal = CommandJournal(path)
    assert journal.get(values[0]).expected == expected
    with pytest.raises(JournalError, match="OPERATION_ID_CONFLICT"):
        journal.begin(*values, {**expected, "storage": "local-lvm"})
    journal.close()


def test_uncertain_submission_requires_reconciliation(tmp_path):
    journal = CommandJournal(tmp_path / "receipts.sqlite3")
    values = identity()
    journal.begin(*values)
    uncertain = journal.transition(
        values[0], from_state="INTENT", to_state="UNCERTAIN", error_code="PROXMOX_CONNECTION_FAILED"
    )
    assert uncertain.error_code == "PROXMOX_CONNECTION_FAILED"
    assert journal.begin(*values)[1] is False
    with pytest.raises(JournalError, match="INVALID_RECEIPT_STATE"):
        journal.transition(values[0], from_state="UNCERTAIN", to_state="SUBMITTED", task_id="UPID")
    journal.transition(
        values[0], from_state="UNCERTAIN", to_state="FAILED", error_code="VERIFIED_ABSENT"
    )
    journal.close()


@pytest.mark.parametrize("change", [{"vmid": True}, {"generation": 0}, {"digest": "wrong"}])
def test_rejects_invalid_identity(tmp_path, change):
    journal = CommandJournal(tmp_path / "receipts.sqlite3")
    keys = ("operation_id", "digest", "kind", "vmid", "runtime_id", "generation")
    values = dict(zip(keys, identity(), strict=True))
    values.update(change)
    with pytest.raises(JournalError, match="INVALID_COMMAND_IDENTITY"):
        journal.begin(**values)
    journal.close()
