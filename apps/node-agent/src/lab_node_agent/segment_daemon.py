"""Root-only local bridge helper; the mTLS agent reaches it over a Unix socket."""

import json
import os
import socket
import stat
import struct
import uuid
from pathlib import Path

from lab_node_agent.segments import STATE_DIR, SegmentError, SegmentManager, SegmentSpec

SOCKET = Path("/run/lab-manager-node/segments.sock")
MAX_REQUEST = 4096


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def dispatch(request: bytes, manager: SegmentManager) -> dict:
    """Only create and inspect owned, down lab bridges; never accept shell text."""
    if not 0 < len(request) <= MAX_REQUEST:
        raise SegmentError("INVALID_SEGMENT_REQUEST")
    try:
        value = json.loads(request, object_pairs_hook=unique_object)
        if not isinstance(value, dict):
            raise ValueError("request object")
        action = value.get("action")
        if action == "create" and set(value) == {"action", "spec"}:
            spec = SegmentSpec.parse(value["spec"])
            bridge = manager.create(spec)
            link = manager.link(spec)
            if link is None or "UP" in link.get("flags", []) or link.get("master"):
                raise SegmentError("SEGMENT_BRIDGE_NOT_CLOSED")
            return {**spec.record(), "bridge": bridge, "state": "CREATED"}
        if action == "get" and set(value) == {"action", "allocation_id"}:
            allocation_id = uuid.UUID(value["allocation_id"])
            record = manager.read().get(str(allocation_id))
            if record is None:
                raise SegmentError("SEGMENT_NOT_FOUND")
            spec = SegmentSpec.parse(
                {field: record[field] for field in ("allocation_id", "mode", "cidr")}
            )
            link = manager.link(spec)
            if link is None:
                raise SegmentError("SEGMENT_BRIDGE_MISSING")
            if link.get("master"):
                raise SegmentError("SEGMENT_BRIDGE_INVALID")
            return {**record, "state": "ACTIVE" if "UP" in link.get("flags", []) else "CREATED"}
    except (TypeError, ValueError, KeyError) as error:
        raise SegmentError("INVALID_SEGMENT_REQUEST") from error
    raise SegmentError("INVALID_SEGMENT_REQUEST")


def read_line(connection: socket.socket) -> bytes:
    chunks = bytearray()
    while len(chunks) <= MAX_REQUEST:
        part = connection.recv(min(1024, MAX_REQUEST + 1 - len(chunks)))
        if not part:
            break
        chunks.extend(part)
        if b"\n" in part:
            if not chunks.endswith(b"\n") or chunks.count(b"\n") != 1:
                raise SegmentError("INVALID_SEGMENT_REQUEST")
            return bytes(chunks[:-1])
    raise SegmentError("INVALID_SEGMENT_REQUEST")


def restore_bridges(manager: SegmentManager) -> None:
    """Restore known bridges after a host reboot, without changing live bridges.

    The persistent allocation file is authoritative. Existing links must still
    prove their Lab Manager alias and bridge type; unknown links are untouched.
    """
    for record in manager.read().values():
        spec = SegmentSpec.parse(
            {field: record[field] for field in ("allocation_id", "mode", "cidr")}
        )
        manager.create(spec)


def serve() -> None:
    import fcntl
    import pwd

    if os.geteuid() != 0:
        raise RuntimeError("ROOT_REQUIRED")
    user = pwd.getpwnam("lab-node-agent")
    directory = SOCKET.parent
    if (
        directory.is_symlink()
        or directory.stat().st_uid != 0
        or directory.stat().st_gid != user.pw_gid
        or directory.stat().st_mode & 0o027
    ):
        raise RuntimeError("SOCKET_DIRECTORY_UNSAFE")
    if SOCKET.exists() or SOCKET.is_symlink():
        details = SOCKET.lstat()
        if not stat.S_ISSOCK(details.st_mode) or details.st_uid != 0:
            raise RuntimeError("SOCKET_PATH_UNSAFE")
        SOCKET.unlink()
    parent = STATE_DIR.parent
    if parent.is_symlink() or parent.stat().st_uid != 0 or parent.stat().st_mode & 0o002:
        raise RuntimeError("STATE_PARENT_UNSAFE")
    STATE_DIR.mkdir(mode=0o700, exist_ok=True)
    if STATE_DIR.is_symlink() or STATE_DIR.stat().st_uid != 0 or STATE_DIR.stat().st_mode & 0o077:
        raise RuntimeError("STATE_DIRECTORY_UNSAFE")
    with (STATE_DIR / "segments.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        restore_bridges(SegmentManager())
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(SOCKET))
        os.chown(SOCKET, 0, user.pw_gid)
        os.chmod(SOCKET, 0o660)
        listener.listen(8)
        while True:
            client, _ = listener.accept()
            with client:
                client.settimeout(5)
                credentials = client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                _, uid, _ = struct.unpack("3i", credentials)
                if uid != user.pw_uid:
                    continue
                try:
                    data = read_line(client)
                    with (STATE_DIR / "segments.lock").open("a+") as lock:
                        fcntl.flock(lock, fcntl.LOCK_EX)
                        result = dispatch(data, SegmentManager())
                    response = {"ok": True, "result": result}
                except (OSError, SegmentError) as error:
                    code = str(error)
                    if not code.isupper() or len(code) > 64:
                        code = "SEGMENT_HELPER_FAILED"
                    response = {"ok": False, "error": code}
                try:
                    client.sendall(json.dumps(response, sort_keys=True).encode() + b"\n")
                except OSError:
                    pass


if __name__ == "__main__":
    serve()
