"""Poll verified dev-proxmox releases and atomically replace the read-only agent."""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPOSITORY = "Egoryich/lab-manager"
BRANCH = "dev-proxmox"
SHA = re.compile(r"[0-9a-f]{40}")
WHEEL = re.compile(r"lab_node_agent-\d+\.\d+\.\d+-py3-none-any\.whl")
BASE = Path("/opt/lab-manager-node")
STATE_DIR = BASE / "update-state"
CURRENT = BASE / "current"
UNITS = (
    "lab-node-agent.service",
    "lab-node-storage-snapshot.service",
    "lab-node-storage-snapshot.timer",
)
ASSETS = set(UNITS) | {
    "node-pki.py",
    "update-node.py",
    "install-node-updater.sh",
    "lab-node-update.service",
    "lab-node-update.timer",
}
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class UpdateError(Exception):
    pass


def run(*args, timeout=180):
    try:
        result = subprocess.run(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise UpdateError(f"COMMAND_FAILED:{args[0]}") from error
    if result.returncode:
        raise UpdateError(f"COMMAND_FAILED:{args[0]}:{result.returncode}")
    return result.stdout.strip()


def api(path):
    request = urllib.request.Request(
        f"https://api.github.com/repos/{REPOSITORY}/{path}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "lab-node-updater"},
    )
    with OPENER.open(request, timeout=20) as response:
        return json.load(response)


def atomic_json(path, value):
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def release_path(sha):
    if not SHA.fullmatch(sha):
        raise UpdateError("INVALID_SHA")
    return BASE / "releases" / sha


def current_sha():
    if not CURRENT.is_symlink():
        raise UpdateError("CURRENT_NOT_SYMLINK")
    target = CURRENT.resolve(strict=True)
    if target != release_path(target.name):
        raise UpdateError("CURRENT_OUTSIDE_RELEASES")
    return target.name


def verified_workflow(sha):
    payload = api(f"actions/runs?branch={BRANCH}&head_sha={sha}&per_page=20")
    runs = [
        item
        for item in payload.get("workflow_runs", [])
        if item.get("head_sha") == sha
        and item.get("head_branch") == BRANCH
        and item.get("event") == "push"
        and item.get("path") == ".github/workflows/check.yml"
        and item.get("repository", {}).get("full_name") == REPOSITORY
    ]
    if not runs:
        return False
    latest = max(runs, key=lambda item: (item["run_number"], item.get("run_attempt", 1)))
    if latest.get("status") != "completed" or latest.get("conclusion") != "success":
        return False
    jobs = api(f"actions/runs/{latest['id']}/jobs?per_page=100").get("jobs", [])
    return all(
        any(job.get("name") == name and job.get("conclusion") == "success" for job in jobs)
        for name in ("identity-groups", "node-agent", "node-agent-release")
    )


def release_assets(sha):
    try:
        release = api(f"releases/tags/node-agent-{sha}")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise
    if release.get("tag_name") != f"node-agent-{sha}" or release.get("target_commitish") != sha:
        raise UpdateError("RELEASE_TARGET_MISMATCH")
    assets = {item["name"]: item for item in release.get("assets", [])}
    wheels = [name for name in assets if WHEEL.fullmatch(name)]
    if len(wheels) != 1 or set(assets) != ASSETS | {"SHA256SUMS", wheels[0]}:
        raise UpdateError("RELEASE_ASSETS_MISMATCH")
    for name, item in assets.items():
        if (
            not re.fullmatch(r"sha256:[0-9a-f]{64}", item.get("digest", ""))
            or item.get("browser_download_url")
            != f"https://github.com/{REPOSITORY}/releases/download/node-agent-{sha}/{name}"
        ):
            raise UpdateError("RELEASE_ASSET_UNVERIFIED")
    return assets, wheels[0]


def verified_candidate(active):
    head = api(f"branches/{BRANCH}")["commit"]["sha"]
    release_path(head)
    if head == active:
        return None
    compare = api(f"compare/{active}...{head}")
    if compare.get("status") != "ahead" or compare.get("base_commit", {}).get("sha") != active:
        raise UpdateError("NON_FORWARD_RELEASE")
    if not verified_workflow(head):
        print(f"Waiting for successful CI: {head}")
        return None
    release = release_assets(head)
    if release is None:
        print(f"Waiting for published release: {head}")
        return None
    return head, *release


def validate_staged(target, assets, wheel):
    if not target.is_dir():
        raise UpdateError("RELEASE_DIRECTORY_INVALID")
    for name, item in assets.items():
        path = target / name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != item[
            "digest"
        ].removeprefix("sha256:"):
            raise UpdateError("STAGED_RELEASE_DIGEST_MISMATCH")
    for name in UNITS:
        if (target / name).read_bytes() != (Path("/etc/systemd/system") / name).read_bytes():
            raise UpdateError("UNIT_CHANGE_REQUIRES_MANUAL_DEPLOYMENT")
    python = target / "venv/bin/python"
    if not python.is_file():
        raise UpdateError("STAGED_WHEEL_MISSING")
    probe = run(str(python), "-m", "lab_node_agent.host_storage")
    if json.loads(probe).get("admission_ready") is not False:
        raise UpdateError("UNEXPECTED_ADMISSION_CHANGE")
    return target


def download_release(sha, assets, wheel):
    target = release_path(sha)
    if target.exists():
        return validate_staged(target, assets, wheel)
    temporary = target.with_name(".incoming-" + sha)
    if temporary.exists():
        raise UpdateError("INCOMPLETE_STAGING_EXISTS")
    temporary.mkdir(mode=0o700)
    try:
        for name, item in assets.items():
            path = temporary / name
            request = urllib.request.Request(
                item["browser_download_url"], headers={"User-Agent": "lab-node-updater"}
            )
            with OPENER.open(request, timeout=60) as response, path.open("wb") as stream:
                digest = hashlib.sha256()
                while chunk := response.read(65536):
                    digest.update(chunk)
                    stream.write(chunk)
                    if stream.tell() > 32 * 1024 * 1024:
                        raise UpdateError("RELEASE_ASSET_TOO_LARGE")
            if digest.hexdigest() != item["digest"].removeprefix("sha256:"):
                raise UpdateError("RELEASE_DIGEST_MISMATCH")
        expected = {}
        for line in (temporary / "SHA256SUMS").read_text().splitlines():
            match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9_.-]+)", line)
            if not match or match.group(2) in expected:
                raise UpdateError("RELEASE_MANIFEST_INVALID")
            expected[match.group(2)] = match.group(1)
        if set(expected) != set(assets) - {"SHA256SUMS"} or any(
            hashlib.sha256((temporary / name).read_bytes()).hexdigest() != digest
            for name, digest in expected.items()
        ):
            raise UpdateError("RELEASE_MANIFEST_MISMATCH")
        run("python3", "-m", "venv", str(temporary / "venv"))
        run(
            str(temporary / "venv/bin/python"),
            "-m",
            "pip",
            "--disable-pip-version-check",
            "install",
            "--no-index",
            "--no-deps",
            str(temporary / wheel),
        )
        temporary.chmod(0o755)
        run("chmod", "-R", "a+rX", str(temporary / "venv"))
        validate_staged(temporary, assets, wheel)
        os.replace(temporary, target)
        return target
    except Exception:
        shutil.rmtree(temporary)
        raise


def switch(sha):
    next_link = BASE / "current.next"
    if next_link.exists() or next_link.is_symlink():
        raise UpdateError("SWITCH_IN_PROGRESS")
    os.symlink(release_path(sha), next_link)
    try:
        os.replace(next_link, CURRENT)
    finally:
        next_link.unlink(missing_ok=True)


def health():
    run("systemctl", "start", "lab-node-storage-snapshot.service")
    if run("systemctl", "show", "-P", "Result", "lab-node-storage-snapshot.service") != "success":
        raise UpdateError("STORAGE_SNAPSHOT_UNHEALTHY")
    run(
        "runuser",
        "-u",
        "lab-node-agent",
        "--",
        str(CURRENT / "venv/bin/python"),
        "-c",
        "from lab_node_agent.host_storage import read_snapshot; read_snapshot()",
    )
    run("systemctl", "restart", "lab-node-agent.service")
    time.sleep(2)
    run("systemctl", "is-active", "--quiet", "lab-node-agent.service")
    run("systemctl", "start", "lab-node-storage-snapshot.timer")
    run("systemctl", "is-active", "--quiet", "lab-node-storage-snapshot.timer")


def deploy(state, sha, assets, wheel):
    active = state["active_sha"]
    if shutil.disk_usage(BASE).free < 512 * 1024**2:
        raise UpdateError("INSUFFICIENT_FREE_SPACE")
    download_release(sha, assets, wheel)
    pending = STATE_DIR / "pending.json"
    atomic_json(pending, {"previous_sha": active, "candidate_sha": sha})
    try:
        run("systemctl", "stop", "lab-node-storage-snapshot.timer")
        run("systemctl", "stop", "lab-node-storage-snapshot.service")
        switch(sha)
        health()
    except Exception as original:
        try:
            run("systemctl", "stop", "lab-node-storage-snapshot.timer")
            switch(active)
            health()
        except Exception as rollback:
            raise UpdateError("ROLLBACK_FAILED_MANUAL_RECOVERY_REQUIRED") from rollback
        state["blocked_sha"] = sha
        atomic_json(STATE_DIR / "state.json", state)
        pending.unlink()
        raise UpdateError("UPDATE_FAILED_ROLLED_BACK") from original
    state["active_sha"] = sha
    state.pop("blocked_sha", None)
    atomic_json(STATE_DIR / "state.json", state)
    pending.unlink()
    print(f"Updated node agent to {sha}")


def main():
    import fcntl

    parser = argparse.ArgumentParser()
    parser.add_argument("--adopt", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise UpdateError("ROOT_REQUIRED")
    STATE_DIR.mkdir(mode=0o700, exist_ok=True)
    with (STATE_DIR / "update.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state_file = STATE_DIR / "state.json"
        pending = STATE_DIR / "pending.json"
        installed = current_sha()
        if args.adopt:
            if pending.exists() or state_file.exists():
                raise UpdateError("ALREADY_ADOPTED_OR_PENDING")
            run("systemctl", "is-active", "--quiet", "lab-node-agent.service")
            run("systemctl", "is-active", "--quiet", "lab-node-storage-snapshot.timer")
            atomic_json(state_file, {"active_sha": installed})
            print(f"Adopted verified running node agent: {installed}")
            return
        if not state_file.exists():
            raise UpdateError("ADOPT_REQUIRED")
        state = json.loads(state_file.read_text())
        active = state.get("active_sha")
        release_path(active)
        if pending.exists():
            saved = json.loads(pending.read_text())
            if saved.get("previous_sha") != active:
                raise UpdateError("PENDING_STATE_MISMATCH")
            if installed == saved.get("candidate_sha"):
                try:
                    health()
                except Exception:
                    try:
                        run("systemctl", "stop", "lab-node-storage-snapshot.timer")
                        switch(active)
                        health()
                    except Exception as rollback:
                        raise UpdateError("PENDING_MANUAL_RECOVERY_REQUIRED") from rollback
                    state["blocked_sha"] = installed
                    atomic_json(state_file, state)
                    pending.unlink()
                    print(f"Recovered previous node release after failure: {active}")
                    return
                state["active_sha"] = installed
                state.pop("blocked_sha", None)
                atomic_json(state_file, state)
                pending.unlink()
                print(f"Recovered completed node update: {installed}")
                return
            if installed == active:
                health()
                state["blocked_sha"] = saved["candidate_sha"]
                atomic_json(state_file, state)
                pending.unlink()
                print(f"Recovered previous node release: {active}")
                return
            raise UpdateError("PENDING_MANUAL_RECOVERY_REQUIRED")
        if installed != active:
            raise UpdateError("INSTALLED_RELEASE_DRIFT")
        candidate = verified_candidate(active)
        if candidate is None:
            print(f"Already current or waiting: {active}")
            return
        sha, assets, wheel = candidate
        if state.get("blocked_sha") == sha:
            raise UpdateError("RELEASE_BLOCKED_AFTER_ROLLBACK")
        if args.check:
            print(f"Verified candidate: {sha}")
            return
        deploy(state, sha, assets, wheel)


if __name__ == "__main__":
    try:
        main()
    except (UpdateError, OSError, ValueError, KeyError, urllib.error.URLError) as error:
        print(f"Update stopped: {type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(1) from None
