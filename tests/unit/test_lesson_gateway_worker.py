import asyncio
import hashlib
import uuid
from types import SimpleNamespace

import pytest
from lab_manager.lesson_gateway_worker import process


class FakeDb:
    def __init__(self, candidates, roster):
        self.results = [candidates, roster]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def execute(self, _query):
        return self.results.pop(0)


class FakeNode:
    def __init__(self, allocation, state):
        self.allocation = allocation
        self.state = state
        self.prepared = []

    def status(self, allocation_id):
        assert allocation_id == self.allocation.id
        return {
            "allocation_id": str(allocation_id),
            "mode": self.allocation.mode,
            "cidr": self.allocation.cidr,
            "state": self.state,
        }

    def prepare_gateway(self, allocation_id, mode, cidr):
        self.prepared.append((allocation_id, mode, cidr))
        return {"state": "GATEWAY_PREPARED"}


@pytest.mark.asyncio
async def test_gateway_is_prepared_once_only_for_complete_lesson_roster(monkeypatch):
    async def immediate(callback, *args):
        return callback(*args)

    monkeypatch.setattr(asyncio, "to_thread", immediate)
    node_id = uuid.uuid4()
    environment_id = uuid.uuid4()
    reservation_id = uuid.uuid4()
    allocation = SimpleNamespace(
        id=uuid.uuid4(),
        node_id=node_id,
        environment_id=environment_id,
        state="APPLIED",
        mode="ISOLATED",
        cidr="10.70.1.0/30",
    )
    run = SimpleNamespace(
        id=uuid.uuid4(),
        node_id=node_id,
        environment_id=environment_id,
        reservation_id=reservation_id,
    )
    operation = SimpleNamespace(
        request_digest=hashlib.sha256(f"LESSON_START:{reservation_id}".encode()).hexdigest()
    )
    endpoint = SimpleNamespace(id=node_id)
    node = FakeNode(allocation, "CREATED")

    def factory(_):
        return node

    def incomplete():
        return FakeDb([(operation, run)], [(allocation.id, allocation), (None, None)])

    assert not await process(incomplete, [endpoint], client_factory=factory)
    assert node.prepared == []

    def complete():
        return FakeDb([(operation, run)], [(allocation.id, allocation)])

    assert await process(complete, [endpoint], client_factory=factory)
    assert node.prepared == [(allocation.id, "ISOLATED", "10.70.1.0/30")]

    node.state = "ACTIVE"
    node.prepared.clear()
    assert not await process(complete, [endpoint], client_factory=factory)
    assert node.prepared == []
