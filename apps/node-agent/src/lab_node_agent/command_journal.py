"""Durable node-side receipt before any Proxmox command is submitted.

An INTENT without a task ID after a crash is uncertain, never replayable by
itself. The caller must reconcile it against Proxmox before proceeding.
"""

import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

DIGEST = re.compile(r"[a-f0-9]{64}")
KINDS = frozenset({"LXC_CREATE", "LXC_START", "LXC_SHUTDOWN"})
STATES = frozenset({"INTENT", "SUBMITTED", "SUCCEEDED", "FAILED", "UNCERTAIN"})
TRANSITIONS = frozenset(
    {
        ("INTENT", "SUBMITTED"),
        ("INTENT", "FAILED"),
        ("INTENT", "UNCERTAIN"),
        ("SUBMITTED", "SUCCEEDED"),
        ("SUBMITTED", "FAILED"),
        ("SUBMITTED", "UNCERTAIN"),
        ("UNCERTAIN", "SUCCEEDED"),
        ("UNCERTAIN", "FAILED"),
    }
)


class JournalError(Exception):
    """A fixed code suitable for a bounded operation response."""


@dataclass(frozen=True)
class Receipt:
    operation_id: uuid.UUID
    digest: str
    kind: str
    vmid: int
    runtime_id: uuid.UUID
    generation: int
    state: str
    task_id: str | None
    error_code: str | None
    expected: dict | None


class CommandJournal:
    def __init__(self, path: Path):
        if not path.is_absolute() or path.is_symlink():
            raise JournalError("INVALID_JOURNAL_PATH")
        self.path = path
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.connection = sqlite3.connect(
            path, timeout=5, isolation_level="IMMEDIATE", check_same_thread=False
        )
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("PRAGMA busy_timeout=5000")
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS receipts (
                operation_id TEXT PRIMARY KEY,
                digest TEXT NOT NULL,
                kind TEXT NOT NULL,
                vmid INTEGER NOT NULL,
                runtime_id TEXT NOT NULL,
                generation INTEGER NOT NULL,
                state TEXT NOT NULL,
                task_id TEXT,
                error_code TEXT,
                expected_json TEXT
            )"""
        )

    def close(self):
        self.connection.close()

    def get(self, operation_id: uuid.UUID) -> Receipt | None:
        if not isinstance(operation_id, uuid.UUID) or operation_id.int == 0:
            raise JournalError("INVALID_OPERATION_ID")
        row = self.connection.execute(
            "SELECT * FROM receipts WHERE operation_id = ?", (str(operation_id),)
        ).fetchone()
        if row is None:
            return None
        receipt = Receipt(
            uuid.UUID(row[0]),
            row[1],
            row[2],
            row[3],
            uuid.UUID(row[4]),
            row[5],
            row[6],
            row[7],
            row[8],
            json.loads(row[9]) if row[9] is not None else None,
        )
        if receipt.state not in STATES:
            raise JournalError("INVALID_RECEIPT_STATE")
        return receipt

    def begin(
        self,
        operation_id: uuid.UUID,
        digest: str,
        kind: str,
        vmid: int,
        runtime_id: uuid.UUID,
        generation: int,
        expected: dict | None = None,
    ) -> tuple[Receipt, bool]:
        if (
            not isinstance(operation_id, uuid.UUID)
            or operation_id.int == 0
            or not isinstance(digest, str)
            or not DIGEST.fullmatch(digest)
            or kind not in KINDS
            or type(vmid) is not int
            or not 100 <= vmid <= 999999999
            or not isinstance(runtime_id, uuid.UUID)
            or runtime_id.int == 0
            or type(generation) is not int
            or not 1 <= generation <= 1000000
            or (expected is not None and not isinstance(expected, dict))
        ):
            raise JournalError("INVALID_COMMAND_IDENTITY")
        expected_json = json.dumps(expected, sort_keys=True) if expected is not None else None
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            busy = self.connection.execute(
                """SELECT operation_id FROM receipts
                WHERE vmid = ? AND operation_id != ?
                  AND state IN ('INTENT','SUBMITTED','UNCERTAIN') LIMIT 1""",
                (vmid, str(operation_id)),
            ).fetchone()
            if busy:
                raise JournalError("VMID_OPERATION_IN_PROGRESS")
            self.connection.execute(
                """INSERT OR IGNORE INTO receipts
                (operation_id,digest,kind,vmid,runtime_id,generation,state,expected_json)
                VALUES (?,?,?,?,?,?,'INTENT',?)""",
                (str(operation_id), digest, kind, vmid, str(runtime_id), generation, expected_json),
            )
            created = self.connection.execute("SELECT changes()").fetchone()[0] == 1
            receipt = self.get(operation_id)
            if (
                receipt.digest != digest
                or receipt.kind != kind
                or receipt.vmid != vmid
                or receipt.runtime_id != runtime_id
                or receipt.generation != generation
                or receipt.expected != expected
            ):
                raise JournalError("OPERATION_ID_CONFLICT")
            return receipt, created

    def transition(
        self,
        operation_id: uuid.UUID,
        *,
        from_state: str,
        to_state: str,
        task_id: str | None = None,
        error_code: str | None = None,
    ) -> Receipt:
        if (from_state, to_state) not in TRANSITIONS:
            raise JournalError("INVALID_RECEIPT_STATE")
        if to_state == "SUBMITTED" and not task_id:
            raise JournalError("TASK_ID_REQUIRED")
        if task_id is not None and (not isinstance(task_id, str) or len(task_id) > 256):
            raise JournalError("INVALID_TASK_ID")
        if error_code is not None and not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", error_code):
            raise JournalError("INVALID_ERROR_CODE")
        with self.connection:
            self.connection.execute(
                """UPDATE receipts SET state = ?, task_id = COALESCE(?, task_id),
                error_code = ? WHERE operation_id = ? AND state = ?""",
                (to_state, task_id, error_code, str(operation_id), from_state),
            )
            if self.connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise JournalError("RECEIPT_STATE_CONFLICT")
            return self.get(operation_id)
