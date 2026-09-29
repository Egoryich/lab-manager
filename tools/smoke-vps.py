"""Exercise release images in a disposable, uniquely named Compose project."""

import argparse
import importlib.util
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import uuid
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-image", required=True)
    parser.add_argument("--web-image", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    (root / ".cache").mkdir(exist_ok=True)
    project = "lab-smoke-" + uuid.uuid4().hex[:12]
    with tempfile.TemporaryDirectory(dir=root / ".cache") as directory:
        env_file = Path(directory) / ".env.smoke"
        subprocess.run(
            [
                sys.executable,
                "tools/init-vps.py",
                "--origin",
                "https://lab.example.org",
                "--revision",
                "0" * 40,
                "--output",
                str(env_file),
            ],
            check=True,
        )
        contents = env_file.read_text().replace(
            "ghcr.io/egoryich/lab-manager-api:" + "0" * 40, args.api_image
        )
        contents = contents.replace("ghcr.io/egoryich/lab-manager-web:" + "0" * 40, args.web_image)
        env_file.write_text(contents)
        command = [
            "docker",
            "compose",
            "-p",
            project,
            "--env-file",
            str(env_file),
            "-f",
            "infra/vps/compose.yml",
        ]

        def compose(*arguments):
            subprocess.run([*command, *arguments], check=True)

        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def request(path, body=None, headers=None, port=18000):
            payload = None if body is None else json.dumps(body).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}{path}",
                data=payload,
                headers={"Content-Type": "application/json", **(headers or {})},
            )
            try:
                response = opener.open(req, timeout=10)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                return response.status, response.headers, response.read()

        try:
            compose("up", "-d", "--wait", "postgres", "redis")
            compose(
                "run",
                "--rm",
                "--no-deps",
                "api",
                "/app/.venv/bin/python",
                "-m",
                "alembic",
                "upgrade",
                "0002_catalog",
            )
            # Upgrade an existing catalog database without losing an existing account.
            compose(
                "exec",
                "-T",
                "postgres",
                "psql",
                "-U",
                "lab",
                "-d",
                "lab",
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                "INSERT INTO users (id, username, display_name, status, auth_revision) "
                "VALUES ('00000000-0000-0000-0000-000000000001', 'migration.probe', "
                "'Migration probe', 'ACTIVE', 1)",
            )
            compose(
                "run",
                "--rm",
                "--no-deps",
                "api",
                "/app/.venv/bin/python",
                "-m",
                "alembic",
                "upgrade",
                "head",
            )
            preserved = subprocess.check_output(
                [
                    *command,
                    "exec",
                    "-T",
                    "postgres",
                    "psql",
                    "-U",
                    "lab",
                    "-d",
                    "lab",
                    "-Atc",
                    "SELECT count(*) FROM users WHERE username='migration.probe'",
                ],
                text=True,
            ).strip()
            assert preserved == "1"
            compose("up", "-d", "--wait", "--wait-timeout", "120", "api", "web", "worker")
            assert request("/api/health/ready")[0] == 200
            status, _, html = request("/groups/example", port=18080)
            assert status == 200 and b'<div id="root">' in html
            password = secrets.token_urlsafe(24)
            body = {"username": "release.smoke", "display_name": "Smoke", "password": password}
            assert request("/api/auth/register", body)[0] == 403
            headers = {"Origin": "https://lab.example.org", "X-Forwarded-Proto": "https"}
            assert request("/api/auth/register", body, headers)[0] == 201
            status, response_headers, data = request(
                "/api/auth/login", {"username": body["username"], "password": password}, headers
            )
            assert status == 200
            cookie = response_headers.get("set-cookie", "")
            assert "__Host-lab_session=" in cookie and "Secure" in cookie and "HttpOnly" in cookie
            assert "STUDENT" in json.loads(data)["user"]["roles"]
            authenticated = {**headers, "Cookie": cookie.split(";", 1)[0]}
            assert request("/api/auth/me", headers=authenticated)[0] == 200
            assert request("/api/admin/users", headers=authenticated)[0] == 403
            compose("restart", "api")
            compose("up", "-d", "--wait", "--wait-timeout", "120", "api")
            assert request("/api/auth/me", headers=authenticated)[0] == 200
            # Exercise the actual updater against only this disposable Compose project.
            spec = importlib.util.spec_from_file_location("updater", root / "tools/update-vps.py")
            updater_module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(updater_module)
            update_root = Path(directory) / "updater"
            (update_root / "repo/infra/vps").mkdir(parents=True)
            shutil.copyfile(
                root / "infra/vps/compose.yml", update_root / "repo/infra/vps/compose.yml"
            )
            shutil.copyfile(env_file, update_root / "repo/.env.vps")
            update = updater_module.Updater(update_root, project=project)
            update.ready = lambda: updater_module.Updater.ready(update, public=False)
            image_ids = {s: update.running_image(s) for s in ("api", "web", "worker")}
            state = {
                "active_sha": "a" * 40,
                "failed_sha": None,
                "compose_hash": updater_module.content_hash(update.compose_file),
                "env_hash": updater_module.content_hash(update.env),
                "schema_revision": update.schema(),
                "images": image_ids,
            }
            update.save_state(state)
            update.apply(
                "b" * 40, state, update.env.read_text(), image_ids["api"], image_ids["web"]
            )
            state = json.loads(update.state_file.read_text())
            assert state["active_sha"] == "b" * 40
            try:
                update.apply(
                    "c" * 40,
                    state,
                    update.env.read_text(),
                    "lab-manager-missing-image:smoke",
                    image_ids["web"],
                )
            except RuntimeError as error:
                assert "previous release restored" in str(error)
            else:
                raise AssertionError("Missing release image must fail")
            assert json.loads(update.state_file.read_text())["failed_sha"] == "c" * 40
            assert update.running_image("api") == image_ids["api"]
            assert update.running_image("worker") == image_ids["worker"]
            assert update.schema() == "0004_nodes"
            assert request("/api/auth/me", headers=authenticated)[0] == 200
            compose("ps")
            print(
                "PASS: release images, migrations, SPA, secure cookie, roles, "
                "restart, update/rollback"
            )
        finally:
            # Only this randomly named disposable project and its test volume are removed.
            compose("down", "--volumes", "--remove-orphans")


if __name__ == "__main__":
    main()
