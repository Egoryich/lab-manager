"""Read-only, single-command VPS check before enabling student networking."""

import argparse
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = ROOT.parent / "update-state"
SHA = re.compile(r"[0-9a-f]{40}")
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def run(*args, timeout=25):
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("LAB_", "COMPOSE_")) and key != "POSTGRES_PASSWORD"
    }
    result = subprocess.run(
        args,
        cwd=ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode:
        raise RuntimeError(f"{args[0]} exited with status {result.returncode}")
    return result.stdout.strip()


def compose(*args):
    return run("docker", "compose", "--env-file", ".env.vps", "-f", "infra/vps/compose.yml", *args)


def as_network(value):
    return ipaddress.ip_network(value if "/" in value else value + "/32", strict=False)


def overlapping_ranges(pool, routes, addresses, docker_networks):
    """Report explicit routes/interfaces/Docker subnets; default routes are not claims."""
    overlaps = []
    for route in routes:
        destination = route.get("dst", "default")
        if destination != "default" and as_network(destination).overlaps(pool):
            overlaps.append(f"route {destination}")
    for interface in addresses:
        for address in interface.get("addr_info", []):
            if address.get("family") != "inet":
                continue
            subnet = as_network(f"{address['local']}/{address['prefixlen']}")
            if subnet.overlaps(pool):
                overlaps.append(f"interface {interface.get('ifname', '?')} {subnet}")
    for network in docker_networks:
        for config in network.get("IPAM", {}).get("Config") or []:
            subnet = config.get("Subnet")
            if subnet and as_network(subnet).overlaps(pool):
                overlaps.append(f"Docker {network.get('Name', '?')} {subnet}")
    return overlaps


def check_addresses(pool):
    routes = json.loads(run("ip", "-j", "-4", "route", "show", "table", "all"))
    addresses = json.loads(run("ip", "-j", "-4", "addr", "show"))
    ids = run("docker", "network", "ls", "-q").splitlines()
    docker_networks = json.loads(run("docker", "network", "inspect", *ids)) if ids else []
    overlaps = overlapping_ranges(pool, routes, addresses, docker_networks)
    if overlaps:
        raise RuntimeError("pool overlaps: " + ", ".join(overlaps))
    return f"{pool} does not overlap VPS routes, interfaces or Docker networks"


def check_release():
    if (STATE_DIR / "pending.json").exists():
        raise RuntimeError("update has an unresolved pending operation")
    state = json.loads((STATE_DIR / "state.json").read_text())
    active = state["active_sha"]
    if not SHA.fullmatch(active) or run("git", "rev-parse", "HEAD") != active:
        raise RuntimeError("active release and repository checkout differ")
    if run("systemctl", "is-active", "lab-manager-update.timer") != "active":
        raise RuntimeError("VPS update timer is inactive")
    properties = run(
        "systemctl",
        "show",
        "--no-pager",
        "lab-manager-update.service",
        "-p",
        "Result",
        "-p",
        "ExecMainStatus",
    )
    if "Result=success" not in properties or "ExecMainStatus=0" not in properties:
        raise RuntimeError("last VPS updater run failed")
    if state["schema_revision"] != "0007_network":
        raise RuntimeError("active release has an older database schema")
    return f"release {active[:12]}, timer active, no pending update"


def check_services():
    state = json.loads((STATE_DIR / "state.json").read_text())
    for service in ("postgres", "redis", "api", "web", "worker"):
        container = compose("ps", "-q", service)
        if not container or "\n" in container:
            raise RuntimeError(f"{service}: expected one container")
        health = run("docker", "inspect", "--format", "{{.State.Health.Status}}", container)
        if health != "healthy":
            raise RuntimeError(f"{service}: {health}")
        if service in ("api", "web", "worker"):
            image = run("docker", "inspect", "--format", "{{.Image}}", container)
            if image != state["images"][service]:
                raise RuntimeError(f"{service}: image differs from active release")
    return "PostgreSQL, Redis, API, web and worker healthy; images match release"


def check_schema():
    revision = compose(
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        "lab",
        "-d",
        "lab",
        "-Atc",
        "SELECT version_num FROM alembic_version",
    )
    if revision != "0007_network":
        raise RuntimeError(f"unexpected database schema {revision}")
    api_revision = compose(
        "exec",
        "-T",
        "api",
        "/app/.venv/bin/python",
        "-c",
        "from lab_manager.schema import CURRENT_SCHEMA_REVISION; print(CURRENT_SCHEMA_REVISION)",
    )
    worker_revision = compose(
        "exec",
        "-T",
        "worker",
        "/app/.venv/bin/python",
        "-c",
        "from lab_manager.schema import CURRENT_SCHEMA_REVISION; print(CURRENT_SCHEMA_REVISION)",
    )
    if api_revision != revision or worker_revision != revision:
        raise RuntimeError("API, worker and database schema differ")
    return f"API, worker and PostgreSQL use {revision}"


def check_readiness():
    values = dict(
        line.split("=", 1)
        for line in (ROOT / ".env.vps").read_text().splitlines()
        if line and not line.startswith("#") and "=" in line
    )
    origin = values["LAB_PUBLIC_ORIGIN"]
    if not origin.startswith("https://"):
        raise RuntimeError("public origin is not HTTPS")
    for url in (
        "http://127.0.0.1:18000/api/health/ready",
        origin + "/api/health/ready",
    ):
        with OPENER.open(url, timeout=10) as response:
            if (
                response.status != 200
                or response.geturl() != url
                or json.load(response) != {"status": "ready"}
            ):
                raise RuntimeError("API readiness failed")
    return "local and public HTTPS API readiness returned 200"


def check_node(management_bridge, guest_bridge):
    code = (
        "import json; from lab_manager.node_transport import load_endpoints,fetch; "
        "e=load_endpoints('/run/lab-node-transport/nodes.json'); "
        "assert len(e)==1; d,_=fetch(e[0]); s=d['sample']; "
        "print(json.dumps({'bridges':s.get('network_bridges'), "
        "'admission_ready':s['admission_ready']}))"
    )
    sample = json.loads(compose("exec", "-T", "worker", "/app/.venv/bin/python", "-c", code))
    bridges = {bridge["name"]: bridge for bridge in sample["bridges"] or []}
    management = bridges.get(management_bridge)
    guest = bridges.get(guest_bridge)
    if not management or not management["active"] or not management["ports"]:
        raise RuntimeError("management bridge is not active with a physical port")
    if not guest or not guest["active"] or guest["ports"]:
        raise RuntimeError("guest bridge is missing, inactive or has a physical port")
    if sample["admission_ready"] is not False:
        raise RuntimeError("node admission state changed unexpectedly")
    return f"mTLS inventory fresh; {guest_bridge} has no physical port; admission remains closed"


def check_space():
    free = shutil.disk_usage(ROOT).free
    if free < 1536 * 1024**2:
        raise RuntimeError("less than 1.5 GiB free for the next VPS update")
    return f"{free / 1024**3:.1f} GiB free"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool", default="10.70.0.0/16")
    parser.add_argument("--management-bridge", default="vmbr0")
    parser.add_argument("--guest-bridge", default="vmbr1")
    args = parser.parse_args()
    if os.name != "posix" or os.geteuid() != 0:
        parser.error("run as root on the VPS")
    pool = ipaddress.ip_network(args.pool, strict=True)
    if pool.version != 4 or not pool.subnet_of(ipaddress.ip_network("10.0.0.0/8")):
        parser.error("the candidate must be an aligned IPv4 subnet within 10.0.0.0/8")
    checks = (
        ("VPS addresses", lambda: check_addresses(pool)),
        ("Updater", check_release),
        ("Services", check_services),
        ("Schema", check_schema),
        ("HTTPS", check_readiness),
        ("Proxmox inventory", lambda: check_node(args.management_bridge, args.guest_bridge)),
        ("Disk", check_space),
    )
    failed = 0
    for name, check in checks:
        try:
            message = check()
        except Exception as error:
            failed += 1
            print(f"FAIL {name}: {error}")
        else:
            print(f"PASS {name}: {message}")
    print(
        f"Result: {'PASS' if not failed else 'FAIL'} ({len(checks) - failed}/{len(checks)} checks)"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
