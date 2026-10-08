#!/usr/bin/env bash
set -euo pipefail

test "$(id -u)" -eq 0
release=${1:?Pass the verified full commit SHA}
digest=${2:?Pass the externally verified updater SHA-256}
[[ "$release" =~ ^[0-9a-f]{40}$ ]]
[[ "$digest" =~ ^[0-9a-f]{64}$ ]]

base=/opt/lab-manager-node/update-state
destination=/usr/local/lib/lab-manager-node/update-node.py
test -f "$destination"
test -f "$base/state.json"
test ! -e "$base/pending.json"
systemctl is-active --quiet lab-node-update.timer

temporary=$(mktemp "$base/updater.XXXXXXXX.py")
next="$destination.next"
next_created=0
cleanup() {
  result=$?
  rm -f "$temporary"
  if [ "$next_created" = 1 ]; then rm -f "$next"; fi
  systemctl start lab-node-update.timer || result=1
  exit "$result"
}
trap cleanup EXIT

curl --fail --silent --show-error --location --retry 3 \
  --connect-timeout 15 --max-time 120 \
  --proto '=https' --proto-redir '=https' \
  -o "$temporary" \
  "https://raw.githubusercontent.com/Egoryich/lab-manager/$release/tools/update-node.py"
printf '%s  %s\n' "$digest" "$temporary" | sha256sum --check --strict
python3 - "$temporary" <<'PY'
import ast
import pathlib
import sys

ast.parse(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
PY

systemctl stop lab-node-update.timer
if systemctl is-active --quiet lab-node-update.service; then
  echo 'Node updater is still running' >&2
  exit 1
fi
exec 9>"$base/update.lock"
flock -n 9
test ! -e "$base/pending.json"
test ! -e "$next"
install -o root -g root -m 0644 "$temporary" "$next"
next_created=1
mv -T "$next" "$destination"
next_created=0
cmp -s "$temporary" "$destination"
echo 'PASS: verified node updater installed; timer restored by cleanup'
