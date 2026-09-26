"""Exercise release images in a disposable, uniquely named Compose project."""

import argparse
import json
import os
import secrets
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
                "head",
            )
            compose("up", "-d", "--wait", "--wait-timeout", "120", "api", "web")
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
            compose("ps")
            print("PASS: release images, migrations, SPA, origin, secure cookie, roles, restart")
        finally:
            # Only this randomly named disposable project and its test volume are removed.
            compose("down", "--volumes", "--remove-orphans")


if __name__ == "__main__":
    main()
