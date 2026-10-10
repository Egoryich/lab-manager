import asyncio
import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from lab_manager import lesson_lxc_start_worker as subject


class FakeDb:
    def __init__(self, result, kind, tracker):
        self.result = result
        self.kind = kind
        self.tracker = tracker

    async def __aenter__(self):
        self.tracker["open"] += 1
        return self

    async def __aexit__(self, *_):
        self.tracker["open"] -= 1
        return False

    async def execute(self, _query):
        assert self.kind == "candidates"
        return self.result

    async def scalars(self, _query):
        assert self.kind == "allocations"
        return self.result


class QueueDb:
    def __init__(
        self,
        *,
        run,
        operation,
        reservation,
        members,
        runtimes,
        bindings,
        allocations,
        disks,
        creates,
    ):
        self.gets = {
            (subject.Operation, operation.id): operation,
            (subject.LessonReservation, reservation.id): reservation,
            **{(subject.ProviderRuntimeBinding, item.runtime_id): item for item in bindings},
            **{(subject.NetworkSegmentAllocation, item.id): item for item in allocations},
        }
        self.scalar_results = iter((run, datetime.now(UTC)))
        results = [members, runtimes, []]
        for disk, create in zip(disks, creates, strict=True):
            results.extend(([disk], [create]))
        self.scalar_lists = iter(results)
        self.added = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    def begin(self):
        return self

    async def execute(self, _query):
        return None

    async def scalar(self, _query):
        return next(self.scalar_results)

    async def scalars(self, _query):
        return next(self.scalar_lists)

    async def get(self, model, key):
        return self.gets.get((model, key))

    def add_all(self, items):
        self.added.extend(items)

    async def flush(self):
        return None


@pytest.mark.asyncio
async def test_lxc_start_waits_for_live_gateway_and_releases_read_session(monkeypatch):
    async def immediate(callback, *args):
        return callback(*args)

    monkeypatch.setattr(asyncio, "to_thread", immediate)
    node_id, run_id, operation_id, allocation_id = (uuid.uuid4() for _ in range(4))
    allocation = SimpleNamespace(
        id=allocation_id,
        node_id=node_id,
        state="APPLIED",
        mode="ISOLATED",
        cidr="10.70.1.0/30",
    )
    tracker = {"open": 0, "queued": []}
    node = SimpleNamespace(state="CREATED")

    def status(value):
        assert value == allocation_id
        return {"state": node.state, "mode": allocation.mode, "cidr": allocation.cidr}

    async def queue(_sessions, *, run_id, operation_id):
        assert tracker["open"] == 0
        tracker["queued"].append((run_id, operation_id))
        return True

    monkeypatch.setattr(subject, "queue_start_commands", queue)
    endpoint = SimpleNamespace(id=node_id)

    def sessions():
        return iter(
            (
                FakeDb([(run_id, operation_id, node_id)], "candidates", tracker),
                FakeDb([allocation], "allocations", tracker),
            )
        )

    for expected in (False, True):
        calls = sessions()
        node.state = "ACTIVE" if expected else "CREATED"
        assert (
            await subject.process(
                lambda calls=calls: next(calls),
                [endpoint],
                client_factory=lambda _: SimpleNamespace(status=status),
            )
            is expected
        )
        assert tracker["open"] == 0
    assert tracker["queued"] == [(run_id, operation_id)]


@pytest.mark.asyncio
async def test_start_commands_are_all_or_none_for_created_roster():
    node_id, environment_id, reservation_id = (uuid.uuid4() for _ in range(3))
    operation_id, run_id, teacher_id = (uuid.uuid4() for _ in range(3))
    run = SimpleNamespace(
        id=run_id,
        state="READY",
        environment_id=environment_id,
        node_id=node_id,
        reservation_id=reservation_id,
    )
    operation = SimpleNamespace(
        id=operation_id,
        environment_id=environment_id,
        kind="LESSON_START",
        state="WAITING_NODE",
        request_digest=hashlib.sha256(f"LESSON_START:{reservation_id}".encode()).hexdigest(),
        owner_teacher_id=teacher_id,
        actor_id=teacher_id,
    )
    reservation = SimpleNamespace(
        id=reservation_id,
        state="ACTIVE",
        ends_at=datetime.now(UTC) + timedelta(hours=1),
        environment_id=environment_id,
        node_id=node_id,
        teacher_id=teacher_id,
    )
    runtimes = [
        SimpleNamespace(
            id=uuid.uuid4(),
            node_id=node_id,
            environment_id=environment_id,
            kind="LXC",
            state="STOPPED",
            network_allocation_id=uuid.uuid4(),
            generation=1,
        )
        for _ in range(2)
    ]
    members = [SimpleNamespace(runtime_id=item.id, state="READY") for item in runtimes]
    bindings = [
        SimpleNamespace(
            runtime_id=item.id,
            node_id=node_id,
            generation=1,
            vmid=901100 + index,
            ownership_marker=f"lab-manager:runtime={item.id};generation=1",
        )
        for index, item in enumerate(runtimes)
    ]
    allocations = [
        SimpleNamespace(
            id=item.network_allocation_id,
            node_id=node_id,
            environment_id=environment_id,
            state="APPLIED",
            mode="ISOLATED",
        )
        for item in runtimes
    ]
    disks = [SimpleNamespace(node_id=node_id, state="PRESENT") for _ in runtimes]
    creates = [
        SimpleNamespace(node_id=node_id, vmid=binding.vmid, generation=1, state="SUCCEEDED")
        for binding in bindings
    ]

    def db():
        return QueueDb(
            run=run,
            operation=operation,
            reservation=reservation,
            members=members,
            runtimes=runtimes,
            bindings=bindings,
            allocations=allocations,
            disks=disks,
            creates=creates,
        )

    disks[1].state = "UNKNOWN"
    failed = db()
    assert not await subject.queue_start_commands(
        lambda: failed, run_id=run_id, operation_id=operation_id
    )
    assert failed.added == []
    assert [item.state for item in runtimes] == ["STOPPED", "STOPPED"]

    disks[1].state = "PRESENT"
    accepted = db()
    assert await subject.queue_start_commands(
        lambda: accepted, run_id=run_id, operation_id=operation_id
    )
    assert len(accepted.added) == 2
    assert {item.kind for item in accepted.added} == {"LXC_START"}
    assert {item.payload["runtime_id"] for item in accepted.added} == {
        str(item.id) for item in runtimes
    }
    assert [item.state for item in runtimes] == ["STARTING", "STARTING"]
