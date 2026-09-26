"""Generate a new deployment env; never overwrite existing credentials."""

import argparse
import base64
import os
import re
import secrets
from pathlib import Path
from urllib.parse import urlsplit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--registry", default="ghcr.io/egoryich")
    parser.add_argument("--output", type=Path, default=Path(".env.vps"))
    args = parser.parse_args()
    origin = urlsplit(args.origin)
    if (
        origin.scheme != "https"
        or not origin.hostname
        or origin.path
        or origin.query
        or origin.fragment
        or origin.username
        or not re.fullmatch(r"https://[a-zA-Z0-9.-]+(?::[0-9]+)?", args.origin)
    ):
        parser.error("origin must be an HTTPS origin without a path")
    if not re.fullmatch(r"[0-9a-f]{40}", args.revision):
        parser.error("revision must be the complete verified Git commit SHA")
    if not re.fullmatch(r"[a-z0-9.-]+/[a-z0-9_-]+", args.registry):
        parser.error("registry must be a lowercase registry/owner")
    password = secrets.token_hex(32)
    encryption_key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    contents = (
        f"LAB_PUBLIC_ORIGIN={args.origin}\n"
        f"LAB_API_IMAGE={args.registry}/lab-manager-api:{args.revision}\n"
        f"LAB_WEB_IMAGE={args.registry}/lab-manager-web:{args.revision}\n"
        f"POSTGRES_PASSWORD={password}\n"
        f"LAB_ENCRYPTION_KEY={encryption_key}\n"
        f"LAB_DIGEST_KEY={secrets.token_hex(48)}\n"
    )
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(contents)
    print(f"Created {args.output}; keep it private and preserve it across updates.")


if __name__ == "__main__":
    main()
