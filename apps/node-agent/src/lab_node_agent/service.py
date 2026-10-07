"""Bounded, read-only mTLS snapshot endpoint. No remote commands or shell execution."""

import argparse
import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import re
import signal
import ssl
import time
import uuid
from pathlib import Path

from lab_node_agent.cli import read_credentials
from lab_node_agent.command_dispatch import CommandDispatcher, CommandError, inspect_lab_bridge
from lab_node_agent.command_journal import CommandJournal
from lab_node_agent.host_storage import StorageProbeError, read_snapshot
from lab_node_agent.inventory import collect
from lab_node_agent.lxc_provider import ProxmoxLxcProvider
from lab_node_agent.proxmox import ProxmoxConfig, ProxmoxReader
from lab_node_agent.segment_client import SegmentClient
from lab_node_agent.segments import SegmentError

logger = logging.getLogger("lab_node_agent")
MAX_BODY = 4 * 1024 * 1024


class SnapshotService:
    def __init__(self, reader, node_id, client_fingerprint, dispatcher=None, segment_client=None):
        self.reader = reader
        self.node_id = str(uuid.UUID(node_id))
        self.boot_id = str(uuid.uuid4())
        if not re.fullmatch(r"[a-f0-9]{64}", client_fingerprint):
            raise ValueError("Invalid client fingerprint")
        self.client_fingerprint = client_fingerprint
        self.body = None
        self.collected_at = 0.0
        self.connections = 0
        self.dispatcher = dispatcher
        self.segment_client = segment_client
        self.command_lock = asyncio.Lock()

    async def refresh(self):
        try:
            try:
                local_storage = await asyncio.to_thread(read_snapshot)
            except StorageProbeError as error:
                local_storage = None
                logger.info("local_storage_unavailable code=%s", error)
            sample = await asyncio.to_thread(collect, self.reader, local_storage)
            body = json.dumps(
                {"node_id": self.node_id, "agent_boot_id": self.boot_id, "sample": sample},
                ensure_ascii=False,
            ).encode()
            if len(body) > MAX_BODY:
                raise ValueError("Snapshot too large")
            self.body = body
            self.collected_at = time.monotonic()
        except Exception as error:
            self.body = None
            logger.warning("collection_failed type=%s", type(error).__name__)

    async def collect_forever(self, stop):
        while not stop.is_set():
            await self.refresh()
            try:
                await asyncio.wait_for(stop.wait(), 30)
            except TimeoutError:
                pass

    async def handle(self, reader, writer):
        self.connections += 1
        try:
            if self.connections > 8:
                return
            tls = writer.get_extra_info("ssl_object")
            cert = tls.getpeercert(binary_form=True) if tls else None
            if not cert or hashlib.sha256(cert).hexdigest() != self.client_fingerprint:
                return
            async with asyncio.timeout(5):
                header = await reader.readuntil(b"\r\n\r\n")
                if len(header) > 8192:
                    return
                lines = header.split(b"\r\n")
                fields = {}
                for line in lines[1:]:
                    if not line:
                        continue
                    if b":" not in line:
                        return
                    name, value = line.split(b":", 1)
                    key = name.strip().lower()
                    if key in fields:
                        return
                    fields[key] = value.strip()
                if b"transfer-encoding" in fields:
                    return
                request_line = lines[0]
                if request_line in (
                    b"POST /v1/commands HTTP/1.1",
                    b"POST /v1/segments HTTP/1.1",
                ):
                    length = fields.get(b"content-length", b"")
                    if len(length) > 5 or not length.isdigit() or not 1 <= int(length) <= 16384:
                        status, body = "400 Bad Request", b'{"error":"INVALID_CONTENT_LENGTH"}'
                        payload = None
                    elif fields.get(b"content-type") != b"application/json":
                        status, body = "400 Bad Request", b'{"error":"INVALID_CONTENT_TYPE"}'
                        payload = None
                    else:
                        payload = await reader.readexactly(int(length))
                else:
                    payload = None
                    if b"content-length" in fields:
                        status, body = "400 Bad Request", b'{"error":"BODY_NOT_ALLOWED"}'
                        request_line = b"GET_WITH_BODY"
            if request_line == b"GET /v1/inventory HTTP/1.1":
                if self.body is None or not 0 <= time.monotonic() - self.collected_at <= 120:
                    status, body = "503 Service Unavailable", b'{"error":"NO_FRESH_SAMPLE"}'
                else:
                    status, body = "200 OK", self.body
            elif request_line == b"POST /v1/commands HTTP/1.1" and payload is not None:
                if self.dispatcher is None:
                    status, body = "503 Service Unavailable", b'{"error":"COMMANDS_DISABLED"}'
                else:
                    try:
                        async with self.command_lock:
                            result = await asyncio.to_thread(self.dispatcher.submit, payload)
                        status, body = "202 Accepted", json.dumps(result).encode()
                    except CommandError as error:
                        status = (
                            "409 Conflict"
                            if error.code in ("OPERATION_ID_CONFLICT", "VMID_OPERATION_IN_PROGRESS")
                            else "400 Bad Request"
                        )
                        body = json.dumps({"error": error.code}).encode()
            elif request_line == b"POST /v1/segments HTTP/1.1" and payload is not None:
                if self.segment_client is None:
                    status, body = "503 Service Unavailable", b'{"error":"SEGMENTS_DISABLED"}'
                else:
                    try:
                        value = json.loads(payload)
                        async with self.command_lock:
                            result = await asyncio.to_thread(self.segment_client.create, value)
                        status, body = "202 Accepted", json.dumps(result).encode()
                    except (ValueError, TypeError):
                        status, body = "400 Bad Request", b'{"error":"INVALID_SEGMENT_SPEC"}'
                    except SegmentError as error:
                        status = (
                            "503 Service Unavailable"
                            if str(error) == "SEGMENT_HELPER_UNAVAILABLE"
                            else "409 Conflict"
                            if str(error) == "SEGMENT_SPEC_CONFLICT"
                            else "400 Bad Request"
                        )
                        body = json.dumps({"error": str(error)}).encode()
            elif request_line.startswith(b"GET /v1/segments/") and request_line.endswith(
                b" HTTP/1.1"
            ):
                if self.segment_client is None:
                    status, body = "503 Service Unavailable", b'{"error":"SEGMENTS_DISABLED"}'
                else:
                    try:
                        allocation_id = uuid.UUID(
                            request_line.removeprefix(b"GET /v1/segments/")
                            .removesuffix(b" HTTP/1.1")
                            .decode("ascii")
                        )
                        async with self.command_lock:
                            result = await asyncio.to_thread(self.segment_client.get, allocation_id)
                        status, body = "200 OK", json.dumps(result).encode()
                    except (UnicodeError, ValueError):
                        status, body = "400 Bad Request", b'{"error":"INVALID_SEGMENT_ID"}'
                    except SegmentError as error:
                        status = (
                            "404 Not Found"
                            if str(error) == "SEGMENT_NOT_FOUND"
                            else "503 Service Unavailable"
                            if str(error) == "SEGMENT_HELPER_UNAVAILABLE"
                            else "400 Bad Request"
                        )
                        body = json.dumps({"error": str(error)}).encode()
            elif request_line.startswith(
                (b"PUT /v1/segments/", b"DELETE /v1/segments/")
            ) and request_line.endswith(b"/gateway HTTP/1.1"):
                if self.segment_client is None:
                    status, body = "503 Service Unavailable", b'{"error":"SEGMENTS_DISABLED"}'
                else:
                    try:
                        method, target, _ = request_line.split(b" ")
                        allocation_id = uuid.UUID(
                            target.removeprefix(b"/v1/segments/")
                            .removesuffix(b"/gateway")
                            .decode("ascii")
                        )
                        method_name = "prepare_gateway" if method == b"PUT" else "close_gateway"
                        async with self.command_lock:
                            result = await asyncio.to_thread(
                                getattr(self.segment_client, method_name), allocation_id
                            )
                        status, body = "200 OK", json.dumps(result).encode()
                    except (UnicodeError, ValueError):
                        status, body = "400 Bad Request", b'{"error":"INVALID_SEGMENT_ID"}'
                    except SegmentError as error:
                        status = (
                            "404 Not Found"
                            if str(error) == "SEGMENT_NOT_FOUND"
                            else "503 Service Unavailable"
                            if str(error) == "SEGMENT_HELPER_UNAVAILABLE"
                            else "409 Conflict"
                        )
                        body = json.dumps({"error": str(error)}).encode()
            elif request_line.startswith(b"GET /v1/commands/") and request_line.endswith(
                b" HTTP/1.1"
            ):
                if self.dispatcher is None:
                    status, body = "503 Service Unavailable", b'{"error":"COMMANDS_DISABLED"}'
                else:
                    try:
                        operation_id = uuid.UUID(
                            request_line.removeprefix(b"GET /v1/commands/")
                            .removesuffix(b" HTTP/1.1")
                            .decode("ascii")
                        )
                        async with self.command_lock:
                            result = await asyncio.to_thread(self.dispatcher.status, operation_id)
                        status, body = "200 OK", json.dumps(result).encode()
                    except (UnicodeError, ValueError):
                        status, body = "400 Bad Request", b'{"error":"INVALID_OPERATION_ID"}'
                    except CommandError as error:
                        status = (
                            "404 Not Found"
                            if error.code == "OPERATION_NOT_FOUND"
                            else "400 Bad Request"
                        )
                        body = json.dumps({"error": error.code}).encode()
            elif (
                request_line
                in (
                    b"POST /v1/commands HTTP/1.1",
                    b"POST /v1/segments HTTP/1.1",
                )
                and payload is None
            ):
                pass  # The bounded header/body parser already set an error.
            elif request_line == b"GET_WITH_BODY":
                pass
            else:
                status, body = "404 Not Found", b'{"error":"NOT_FOUND"}'
            writer.write(
                f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\nCache-Control: no-store\r\n"
                "Connection: close\r\n\r\n".encode()
                + body
            )
            await writer.drain()
        except (
            OSError,
            ValueError,
            TimeoutError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
        ):
            pass
        finally:
            self.connections -= 1
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 2)
            except (OSError, TimeoutError):
                pass


def server_context(ca, certificate, key):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cafile=str(ca))
    context.load_cert_chain(str(certificate), str(key))
    return context


async def run(config_path):
    config = json.loads(config_path.read_text(encoding="utf-8"))
    address = ipaddress.ip_address(config["listen_address"])
    if address.is_unspecified or address.is_multicast or address.is_global:
        raise ValueError("Bind only to the configured private/tailnet address")
    port = config["listen_port"]
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("Invalid port")
    credentials = Path(os.environ["CREDENTIALS_DIRECTORY"])
    reader = ProxmoxReader(
        ProxmoxConfig(
            config["proxmox_origin"],
            config["proxmox_node"],
            credentials / "proxmox-ca.pem",
            **read_credentials(credentials / "proxmox-token.json"),
        )
    )
    dispatcher = journal = None
    commands_enabled = config.get("commands_enabled", False)
    if type(commands_enabled) is not bool:
        raise ValueError("Invalid command mode")
    if commands_enabled:
        journal = CommandJournal(Path("/var/lib/lab-manager-commands/receipts.sqlite3"))
        dispatcher = CommandDispatcher(
            node_id=uuid.UUID(config["node_id"]),
            template=config["command_template"],
            storage=config["command_storage"],
            pool=config["command_pool"],
            journal=journal,
            provider=ProxmoxLxcProvider(
                ProxmoxConfig(
                    config["proxmox_origin"],
                    config["proxmox_node"],
                    credentials / "proxmox-ca.pem",
                    **read_credentials(credentials / "proxmox-write-token.json"),
                )
            ),
            inspect_bridge=inspect_lab_bridge,
        )
    segments_enabled = config.get("segments_enabled", False)
    if type(segments_enabled) is not bool:
        raise ValueError("Invalid segment mode")
    service = SnapshotService(
        reader,
        config["node_id"],
        config["client_sha256"],
        dispatcher,
        SegmentClient() if segments_enabled else None,
    )
    context = server_context(
        credentials / "transport-ca.pem", credentials / "server.pem", credentials / "server.key"
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    collector = asyncio.create_task(service.collect_forever(stop))
    try:
        async with await asyncio.start_server(
            service.handle,
            str(address),
            port,
            ssl=context,
            ssl_handshake_timeout=5,
            ssl_shutdown_timeout=2,
            limit=8192,
            backlog=16,
        ):
            await stop.wait()
    finally:
        stop.set()
        await collector
        if journal:
            journal.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(run(args.config))
    except Exception as error:
        logger.error("service_failed type=%s", type(error).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
