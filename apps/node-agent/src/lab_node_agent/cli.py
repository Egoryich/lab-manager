import argparse
import json
import os
import stat
import sys
from pathlib import Path

from lab_node_agent.inventory import collect
from lab_node_agent.proxmox import InventoryError, ProxmoxConfig, ProxmoxReader


def read_credentials(path):
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
        raise InventoryError("INVALID_CREDENTIAL_FILE")
    if os.name == "posix" and stat.S_IMODE(info.st_mode) & 0o007:
        raise InventoryError("CREDENTIAL_FILE_WORLD_ACCESSIBLE")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) != {"token_id", "token_secret"}:
        raise InventoryError("INVALID_CREDENTIAL_FILE")
    if not all(isinstance(value, str) for value in data.values()):
        raise InventoryError("INVALID_CREDENTIAL_FILE")
    return data


def main():
    parser = argparse.ArgumentParser(
        description="Read-only Proxmox inventory; no changes to guests"
    )
    parser.add_argument("--origin", required=True, help="https://certificate-hostname:8006")
    parser.add_argument("--node", required=True)
    parser.add_argument("--ca-file", type=Path, required=True)
    parser.add_argument("--credentials-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        credentials = read_credentials(args.credentials_file)
        reader = ProxmoxReader(ProxmoxConfig(args.origin, args.node, args.ca_file, **credentials))
        result = collect(reader)
    except InventoryError as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError, RecursionError):
        print('{"error":"INVALID_LOCAL_CONFIGURATION"}', file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
