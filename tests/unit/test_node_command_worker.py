"""A recovered worker may inspect a receipt but must not repeat a POST."""

import uuid

from lab_manager.node_command_worker import CommandClaim, receipt_state


def ticket():
    command_id = uuid.uuid4()
    payload = {
        "operation_id": str(command_id),
        "runtime_id": str(uuid.uuid4()),
        "kind": "LXC_CREATE",
        "generation": 1,
        "vmid": 901001,
    }
    return CommandClaim(command_id, 1, uuid.uuid4(), uuid.uuid4(), True, "QUEUED", payload)


def receipt(claim, state):
    return {
        "operation_id": str(claim.id),
        "runtime_id": claim.payload["runtime_id"],
        "kind": claim.payload["kind"],
        "generation": claim.payload["generation"],
        "vmid": claim.payload["vmid"],
        "state": state,
        "error_code": None,
    }


def test_success_requires_identity_match():
    claim = ticket()
    assert receipt_state(receipt(claim, "SUCCEEDED"), claim) == ("SUCCEEDED", None)
    foreign = receipt(claim, "SUCCEEDED")
    foreign["runtime_id"] = str(uuid.uuid4())
    assert receipt_state(foreign, claim) == (
        "UNCERTAIN",
        "NODE_COMMAND_RECEIPT_MISMATCH",
    )


def test_failed_node_task_requires_reconciliation():
    claim = ticket()
    assert receipt_state(receipt(claim, "FAILED"), claim) == (
        "UNCERTAIN",
        "NODE_RECONCILIATION_REQUIRED",
    )
    assert receipt_state(receipt(claim, "SUBMITTED"), claim) == ("SUBMITTED", None)
