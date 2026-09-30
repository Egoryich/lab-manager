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
from lab_node_agent.host_storage import StorageProbeError, read_snapshot
from lab_node_agent.inventory import collect
from lab_node_agent.proxmox import ProxmoxConfig, ProxmoxReader

logger = logging.getLogger("lab_node_agent")
MAX_BODY = 4 * 1024 * 1024


class SnapshotService:
    def __init__(self, reader, node_id, client_fingerprint):
        self.reader = reader
        self.node_id = str(uuid.UUID(node_id))
        self.boot_id = str(uuid.uuid4())
        if not re.fullmatch(r"[a-f0-9]{64}", client_fingerprint):
            raise ValueError("Invalid client fingerprint")
        self.client_fingerprint = client_fingerprint
        self.body = None
        self.collected_at = 0.0
        self.connections = 0

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
                if lines[0] != b"GET /v1/inventory HTTP/1.1":
                    status, body = "404 Not Found", b'{"error":"NOT_FOUND"}'
                elif any(
                    line.lower().startswith((b"transfer-encoding:", b"content-length:"))
                    for line in lines[1:]
                ):
                    status, body = "400 Bad Request", b'{"error":"BODY_NOT_ALLOWED"}'
                elif self.body is None or not 0 <= time.monotonic() - self.collected_at <= 120:
                    status, body = "503 Service Unavailable", b'{"error":"NO_FRESH_SAMPLE"}'
                else:
                    status, body = "200 OK", self.body
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
    service = SnapshotService(reader, config["node_id"], config["client_sha256"])
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
