#!/usr/bin/env bash
# One-time, fail-closed command identity for the Lab Manager Proxmox agent.
set -euo pipefail
umask 077

test "$(id -u)" -eq 0
base=/opt/lab-manager-node
config=/etc/lab-manager-node/service.json
unit=/etc/systemd/system/lab-node-agent.service
dropin=/etc/systemd/system/lab-node-agent.service.d/commands.conf
credential=/etc/lab-manager/proxmox-write-token.json
created=/etc/lab-manager/command-token-created.json
stage=$(readlink -f "$base/current")
test "$stage" = "$base/releases/$(python3 -c 'import json; print(json.load(open("/opt/lab-manager-node/update-state/state.json"))["active_sha"])')"
test ! -e "$base/update-state/pending.json"
test -f "$stage/SHA256SUMS"
(cd "$stage" && sha256sum --check --strict SHA256SUMS >/dev/null)
test "$($stage/venv/bin/python -c 'from importlib.metadata import version; print(version("lab-node-agent"))')" = 0.18.0
systemctl is-active --quiet lab-node-agent.service lab-node-segment-helper.service lab-node-network-guard.service
test ! -e "$credential"
test ! -e "$created"
test -f "$config"
test -f "$unit"
cmp "$stage/lab-node-agent.service" "$unit"
test ! -e "$dropin"
test -s /etc/pve/pve-root-ca.pem
pvesm list local --content vztmpl | grep -Fq 'local:vztmpl/debian-13-standard_13.6-1_amd64.tar.zst'
pvesm status | awk '$1 == "student-lvm" && $3 == "active" {found=1} END {exit !found}'
python3 - "$config" <<'PY'
import json
import sys

config = json.load(open(sys.argv[1]))
assert config.get("segments_enabled") is True
assert not any(key in config for key in (
    "commands_enabled", "command_template", "command_storage", "command_pool"
))
PY
for role in LabCommandGuest LabCommandDisk LabCommandTemplate LabCommandBridge; do
  pveum role list --output-format json |
    python3 -c 'import json,sys; raise SystemExit(any(x["roleid"]==sys.argv[1] for x in json.load(sys.stdin)))' "$role"
done
pveum user list --output-format json |
  python3 -c 'import json,sys; raise SystemExit(any(x["userid"]=="lab-command@pve" for x in json.load(sys.stdin)))'
pveum pool list --output-format json |
  python3 -c 'import json,sys; raise SystemExit(any(x.get("poolid")=="lab-manager" for x in json.load(sys.stdin)))'

# The pool confines VM privileges; the agent independently rejects non-lmbr networks.
pveum pool add lab-manager --comment 'Lab Manager student guests'
pveum role add LabCommandGuest --privs \
  'VM.Allocate VM.Audit VM.Config.CPU VM.Config.Memory VM.Config.Disk VM.Config.Network VM.Config.Options VM.PowerMgmt'
pveum role add LabCommandDisk --privs 'Datastore.Audit Datastore.AllocateSpace'
pveum role add LabCommandTemplate --privs 'Datastore.Audit'
pveum role add LabCommandBridge --privs 'SDN.Use'
pveum user add lab-command@pve --comment 'Lab Manager controlled guest commands'
pveum acl modify /pool/lab-manager --users lab-command@pve --roles LabCommandGuest --propagate 1
pveum acl modify /storage/student-lvm --users lab-command@pve --roles LabCommandDisk --propagate 1
pveum acl modify /storage/local --users lab-command@pve --roles LabCommandTemplate --propagate 1
pveum acl modify /sdn/zones/localnetwork --users lab-command@pve --roles LabCommandBridge --propagate 1

(set -C; pveum user token add lab-command@pve commands --privsep 1 --output-format json > "$created")
python3 - "$created" "$credential" <<'PY'
import json
import os
import pathlib
import sys

created, credential = map(pathlib.Path, sys.argv[1:])
data = json.loads(created.read_text())
assert data["full-tokenid"] == "lab-command@pve!commands"
assert isinstance(data["value"], str) and len(data["value"]) >= 32
with credential.open("x") as output:
    json.dump({"token_id": data["full-tokenid"], "token_secret": data["value"]}, output)
    output.flush()
    os.fsync(output.fileno())
created.unlink()
PY
for assignment in \
  '/pool/lab-manager LabCommandGuest' \
  '/storage/student-lvm LabCommandDisk' \
  '/storage/local LabCommandTemplate' \
  '/sdn/zones/localnetwork LabCommandBridge'; do
  read -r path role <<< "$assignment"
  pveum acl modify "$path" --tokens 'lab-command@pve!commands' --roles "$role" --propagate 1
done
python3 - <<'PY'
import json
import subprocess

checks = {
    "/pool/lab-manager": {"VM.Allocate", "VM.Audit", "VM.Config.Network", "VM.PowerMgmt"},
    "/storage/student-lvm": {"Datastore.Audit", "Datastore.AllocateSpace"},
    "/storage/local": {"Datastore.Audit"},
    "/sdn/zones/localnetwork": {"SDN.Use"},
}
for path, needed in checks.items():
    result = subprocess.run(
        ["pveum", "user", "token", "permissions", "lab-command@pve", "commands",
         "--path", path, "--output-format", "json"],
        check=True, capture_output=True, text=True,
    )
    payload = json.loads(result.stdout)
    granted = payload.get(path, {})
    assert all(granted.get(privilege) == 1 for privilege in needed), path
print("Proxmox token permissions verified; token value not displayed.")
PY

config_backup=$(mktemp /etc/lab-manager-node/service.backup.XXXXXX)
cp -p "$config" "$config_backup"
activated=0
restore() {
  if [ "$activated" = 0 ]; then
    cp -p "$config_backup" "$config"
    rm -f "$dropin"
    systemctl daemon-reload
    systemctl restart lab-node-agent.service || true
    echo 'Agent configuration restored; command identity remains for inspection.' >&2
  fi
  rm -f "$config_backup"
}
trap restore EXIT

python3 - "$config" <<'PY'
import json
import os
import pathlib
import sys
import tempfile

path = pathlib.Path(sys.argv[1])
original = path.stat()
config = json.loads(path.read_text())
config.update({
    "commands_enabled": True,
    "command_template": "local:vztmpl/debian-13-standard_13.6-1_amd64.tar.zst",
    "command_storage": "student-lvm",
    "command_pool": "lab-manager",
})
fd, name = tempfile.mkstemp(prefix="service.", dir=path.parent)
try:
    os.fchown(fd, original.st_uid, original.st_gid)
    os.fchmod(fd, original.st_mode & 0o777)
    with os.fdopen(fd, "w") as output:
        json.dump(config, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(name, path)
finally:
    if os.path.exists(name):
        os.unlink(name)
PY
install -d -o root -g root -m 0755 "${dropin%/*}"
cat > "$dropin" <<'UNIT'
[Service]
LoadCredential=proxmox-write-token.json:/etc/lab-manager/proxmox-write-token.json
StateDirectory=lab-manager-commands
StateDirectoryMode=0700
UNIT
chmod 0644 "$dropin"
systemctl daemon-reload
systemctl restart lab-node-agent.service
systemctl is-active --quiet lab-node-agent.service
test "$(runuser -u lab-node-agent -- "$stage/venv/bin/python" -c \
  'from lab_node_agent.command_journal import CommandJournal; from pathlib import Path; j=CommandJournal(Path("/var/lib/lab-manager-commands/receipts.sqlite3")); j.close(); print("OK")')" = OK
activated=1
echo 'PASS: scoped command token and agent command journal enabled; no guest was created'
