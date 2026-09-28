"""Pull verified dev-vps application images; never migrate or prune data unattended."""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPOSITORY = "Egoryich/lab-manager"
BRANCH = "dev-vps"
SHA = re.compile(r"[0-9a-f]{40}")
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def run(*args, cwd=None):
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("LAB_", "COMPOSE_")) and key != "POSTGRES_PASSWORD"
    }
    result = subprocess.run(
        args,
        cwd=cwd,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=480,
    )
    if result.returncode:
        # Compose errors can contain interpolated environment values; do not journal them.
        raise RuntimeError(f"{args[0]} failed (exit {result.returncode}); inspect services locally")
    return result.stdout.strip()


def atomic(path, content):
    temp = path.with_suffix(path.suffix + ".tmp")
    descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
    if os.name != "nt":
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def fields(contents):
    result = {}
    for line in contents.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or key in result:
            raise RuntimeError("Invalid or duplicate deployment env entry")
        result[key] = value
    return result


def image_env(contents, api, web):
    values = fields(contents)
    if not {"LAB_API_IMAGE", "LAB_WEB_IMAGE"} <= values.keys():
        raise RuntimeError("Image settings missing")
    replacements = {"LAB_API_IMAGE": api, "LAB_WEB_IMAGE": web}
    return (
        "\n".join(
            f"{line.partition('=')[0]}={replacements[line.partition('=')[0]]}"
            if line.partition("=")[0] in replacements
            else line
            for line in contents.splitlines()
        )
        + "\n"
    )


def verified_run(payload, sha):
    runs = [
        r
        for r in payload.get("workflow_runs", [])
        if r.get("head_sha") == sha
        and r.get("head_branch") == BRANCH
        and r.get("event") == "push"
        and r.get("path") == ".github/workflows/check.yml"
        and r.get("repository", {}).get("full_name") == REPOSITORY
    ]
    if not runs:
        return None
    latest = max(runs, key=lambda r: (r["run_number"], r.get("run_attempt", 1)))
    if latest.get("status") == "completed" and latest.get("conclusion") == "success":
        return latest
    return None


def github_json(path):
    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPOSITORY}/{path}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "lab-manager-updater"},
    )
    with OPENER.open(req, timeout=20) as response:
        return json.load(response)


def content_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pulled_digest(repository, output, known_digests):
    # One image ID may have several RepoDigests. Pin the digest reported by THIS pull.
    digests = re.findall(r"(?m)^Digest: (sha256:[0-9a-f]{64})\s*$", output)
    if len(digests) != 1 or f"{repository}@{digests[0]}" not in known_digests:
        raise RuntimeError("Cannot verify the digest of this image pull")
    return f"{repository}@{digests[0]}"


class Updater:
    def __init__(self, root, project="lab-manager"):
        self.root = root
        self.repo = root / "repo"
        self.env = self.repo / ".env.vps"
        self.compose_file = self.repo / "infra/vps/compose.yml"
        self.directory = root / "update-state"
        self.directory.mkdir(mode=0o700, exist_ok=True)
        self.state_file = self.directory / "state.json"
        self.pending = self.directory / "pending.json"
        self.previous = self.directory / "previous.env"
        self.candidate = self.directory / "candidate.env"
        self.project = project

    def compose(self, env, *args):
        return run(
            "docker",
            "compose",
            "-p",
            self.project,
            "--env-file",
            str(env),
            "-f",
            str(self.compose_file),
            *args,
            cwd=self.repo,
        )

    def git(self, *args):
        return run("git", *args, cwd=self.repo)

    def save_state(self, state):
        atomic(self.state_file, json.dumps(state, indent=2) + "\n")

    def ready(self, public=True):
        origin = fields(self.env.read_text())["LAB_PUBLIC_ORIGIN"]
        urls = ["http://127.0.0.1:18000/api/health/ready", "http://127.0.0.1:18080/"]
        if public:
            urls.append(origin + "/api/health/ready")
        for attempt in range(3):
            try:
                for url in urls:
                    with OPENER.open(url, timeout=8) as response:
                        if response.status != 200 or response.geturl() != url:
                            raise RuntimeError("Unexpected readiness response")
                        if url.endswith("/ready") and json.load(response) != {"status": "ready"}:
                            raise RuntimeError("API is not ready")
                return
            except Exception:
                if attempt == 2:
                    raise RuntimeError(
                        "Readiness failed; check local API, frontend and HTTPS"
                    ) from None
                time.sleep(2)

    def running_image(self, service):
        container = self.compose(self.env, "ps", "-q", service)
        if not container or "\n" in container:
            raise RuntimeError("Expected one running container per application service")
        image = run("docker", "inspect", "--format", "{{.Image}}", container)
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
            raise RuntimeError("Cannot pin previous container image")
        return image

    def schema(self):
        return self.compose(
            self.env,
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

    def state(self, adopt=False):
        if self.state_file.exists() and not adopt:
            return json.loads(self.state_file.read_text())
        values = fields(self.env.read_text())
        sha = values["LAB_API_IMAGE"].rsplit(":", 1)[-1]
        if not SHA.fullmatch(sha) or not values["LAB_WEB_IMAGE"].endswith(":" + sha):
            raise RuntimeError("Bootstrap requires both images tagged with the same full Git SHA")
        if self.git("rev-parse", "HEAD") != sha:
            raise RuntimeError("Bootstrap checkout must match the currently installed images")
        self.git("diff", "--exit-code", "HEAD", "--", "infra/vps/compose.yml")
        self.ready()
        for service, key in (("api", "LAB_API_IMAGE"), ("web", "LAB_WEB_IMAGE")):
            container = self.compose(self.env, "ps", "-q", service)
            if run("docker", "inspect", "--format", "{{.Config.Image}}", container) != values[key]:
                raise RuntimeError("Running containers do not match deployment env")
        state = {
            "active_sha": sha,
            "failed_sha": None,
            "compose_hash": content_hash(self.compose_file),
            "env_hash": content_hash(self.env),
            "schema_revision": self.schema(),
            "images": {service: self.running_image(service) for service in ("api", "web")},
        }
        self.save_state(state)
        return state

    def recover(self):
        if not self.pending.exists():
            return
        pending = json.loads(self.pending.read_text())
        if content_hash(self.compose_file) != pending["previous_state"]["compose_hash"]:
            raise RuntimeError(
                "Compose changed during interrupted update; manual recovery required"
            )
        if self.schema() != pending["previous_state"]["schema_revision"]:
            raise RuntimeError("Database revision changed; automatic image rollback refused")
        print("Recovering previous application images; database schema is unchanged", flush=True)
        self.compose(
            self.previous,
            "up",
            "-d",
            "--no-deps",
            "--pull",
            "never",
            "--wait",
            "--wait-timeout",
            "120",
            "api",
            "web",
        )
        atomic(self.env, self.previous.read_text())
        self.ready()
        state = pending["previous_state"]
        state["failed_sha"] = pending["target_sha"]
        state["env_hash"] = content_hash(self.env)
        self.save_state(state)
        self.pending.unlink()
        print("Previous application restored; failed release is blocked", flush=True)

    def apply(self, sha, state, contents, api, web):
        previous = image_env(contents, self.running_image("api"), self.running_image("web"))
        atomic(self.previous, previous)
        atomic(self.candidate, image_env(contents, api, web))
        if self.env.read_text() != contents:
            raise RuntimeError("Deployment env changed concurrently; stopped")
        atomic(self.pending, json.dumps({"previous_state": state, "target_sha": sha}))
        try:
            self.compose(
                self.candidate,
                "up",
                "-d",
                "--no-deps",
                "--pull",
                "never",
                "--wait",
                "--wait-timeout",
                "120",
                "api",
                "web",
            )
            self.ready()
            atomic(self.env, self.candidate.read_text())
            self.save_state(
                {
                    **state,
                    "active_sha": sha,
                    "failed_sha": None,
                    "env_hash": content_hash(self.env),
                    "images": {s: self.running_image(s) for s in ("api", "web")},
                }
            )
            self.pending.unlink()
            print(f"Updated application to {sha}", flush=True)
        except Exception:
            self.recover()
            raise RuntimeError("Update failed; previous release restored") from None

    def tick(self, check=False, retry=False):
        if self.pending.exists():
            if check:
                print("Interrupted update requires recovery; run without --check")
                return
            self.recover()
            return
        state = self.state()
        if content_hash(self.compose_file) != state["compose_hash"]:
            raise RuntimeError("Compose changed locally; manual review required")
        if content_hash(self.env) != state["env_hash"]:
            raise RuntimeError(
                "Deployment env changed locally; manual review and adoption required"
            )
        if self.schema() != state["schema_revision"]:
            raise RuntimeError("Database revision changed; manual adoption required")
        if any(self.running_image(s) != state["images"][s] for s in ("api", "web")):
            raise RuntimeError("Running images changed outside updater; manual adoption required")
        remote = self.git("remote", "get-url", "origin")
        if remote != f"https://github.com/{REPOSITORY}.git":
            raise RuntimeError("Unexpected Git remote; refusing to deploy")
        self.git("fetch", "--no-tags", "origin", BRANCH)
        sha = self.git("rev-parse", "FETCH_HEAD")
        if not SHA.fullmatch(sha):
            raise RuntimeError("Invalid fetched revision")
        if sha == state["active_sha"]:
            print(f"Already current: {sha}")
            return
        if sha == state.get("failed_sha") and not retry:
            print(f"Release blocked after failure: {sha}; inspect before using --retry")
            return
        self.git("merge-base", "--is-ancestor", state["active_sha"], sha)
        path = (
            "actions/workflows/check.yml/runs"
            f"?branch={BRANCH}&event=push&head_sha={sha}&per_page=20"
        )
        ci_run = verified_run(github_json(path), sha)
        if ci_run is None:
            print(f"Waiting for successful CI and image publication: {sha}")
            return
        jobs = github_json(f"actions/runs/{ci_run['id']}/jobs?per_page=100")
        successful = {
            j["name"]
            for j in jobs.get("jobs", [])
            if j.get("status") == "completed" and j.get("conclusion") == "success"
        }
        if not {"identity-groups", "vps-images"} <= successful:
            raise RuntimeError("Required test or image publication job did not succeed")
        for path in ("infra/vps/compose.yml", "migrations"):
            if self.git("rev-parse", f"{sha}:{path}") != self.git(
                "rev-parse", f"{state['active_sha']}:{path}"
            ):
                raise RuntimeError("Release changes schema or Compose; manual deployment required")
        print(f"Verified compatible release: {sha}", flush=True)
        if check:
            return
        if shutil.disk_usage(self.root).free < 1536 * 1024 * 1024:
            raise RuntimeError("Less than 1.5 GiB free; refusing image download")
        self.ready()
        contents = self.env.read_text()
        images = []
        for service in ("api", "web"):
            repository = f"ghcr.io/egoryich/lab-manager-{service}"
            tag = f"{repository}:{sha}"
            pull_output = run("docker", "pull", tag)
            digests = json.loads(
                run("docker", "image", "inspect", "--format", "{{json .RepoDigests}}", tag)
            )
            images.append(pulled_digest(repository, pull_output, digests))
        if shutil.disk_usage(self.root).free < 512 * 1024 * 1024:
            raise RuntimeError("Insufficient free space after pull; current service kept")
        self.apply(sha, state, contents, *images)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/opt/lab-manager"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--retry", action="store_true")
    mode.add_argument("--adopt", action="store_true")
    args = parser.parse_args()
    if sys.platform != "linux" or os.geteuid() != 0:
        raise SystemExit("Run on Linux as root")
    import fcntl

    updater = Updater(args.root.resolve())
    with (updater.directory / "update.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another updater holds the lock; skipped")
            return
        if args.adopt:
            if updater.pending.exists():
                raise RuntimeError("Recover interrupted update before adopting a manual deployment")
            updater.state(adopt=True)
            print("Adopted manually verified running release")
        else:
            updater.tick(check=args.check, retry=args.retry)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Update stopped: {type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(1) from None
