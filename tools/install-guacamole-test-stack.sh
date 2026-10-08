#!/usr/bin/env bash
# Bring up the private JSON-auth Guacamole protocol test; never publish it.
set -euo pipefail
umask 077

if [ "$(id -u)" -ne 0 ] || [ "$#" -ne 0 ]; then
    echo 'Run as root in the Guacamole VM, without arguments' >&2
    exit 2
fi
. /etc/os-release
test "$ID" = debian && test "$VERSION_ID" = 13
test "$(hostname -s)" = lab-guacamole

bind_ip=$(tailscale ip -4)
python3 - "$bind_ip" <<'PY'
import ipaddress
import sys

address = ipaddress.IPv4Address(sys.argv[1])
assert address in ipaddress.IPv4Network("100.64.0.0/10"), "Headscale IPv4 is required"
PY
systemctl is-active --quiet docker.service tailscaled.service

directory=/opt/lab-manager-guacamole
install -d -o root -g root -m 0700 "$directory"
candidate=$(mktemp "$directory/compose.XXXXXX")
trap 'rm -f "$candidate"' EXIT
curl --fail --silent --show-error --location --retry 3 \
    --connect-timeout 15 --max-time 120 \
    --proto '=https' --proto-redir '=https' \
    -o "$candidate" \
    'https://raw.githubusercontent.com/Egoryich/lab-manager/f121b8c4d6f8436b0702ad91d8e53de19e603d48/infra/guacamole/compose.yml'
printf '%s  %s\n' \
    '307b42490d650a107057efae62d69ecdcd30350b664e2ef088c462c253307f1c' \
    "$candidate" | sha256sum --check --strict >/dev/null

if [ -e "$directory/compose.yml" ]; then
    cmp "$candidate" "$directory/compose.yml"
else
    install -o root -g root -m 0600 "$candidate" "$directory/compose.yml"
fi

if [ -e "$directory/.env" ]; then
    grep -Fxq "GUAC_BIND_IP=$bind_ip" "$directory/.env" || {
        echo 'Existing gateway bind differs; inspect before changing it' >&2
        exit 1
    }
else
    printf 'GUAC_BIND_IP=%s\nJSON_SECRET_KEY=%s\n' \
        "$bind_ip" "$(openssl rand -hex 16)" > "$directory/.env"
    chmod 0600 "$directory/.env"
fi

cd "$directory"
docker compose --env-file .env -f compose.yml config --quiet
docker compose --env-file .env -f compose.yml up -d --wait --wait-timeout 120

for attempt in $(seq 1 20); do
    if curl --fail --silent --show-error --max-time 3 \
        -o /dev/null "http://$bind_ip:8080/guacamole/" 2>/dev/null; then
        echo 'PASS: private Guacamole test stack is reachable on the VM tailnet IP'
        echo 'This is a protocol test only; public proxy and student access remain disabled.'
        exit 0
    fi
    sleep 2
done
echo 'Guacamole did not become ready; inspect docker compose ps/logs' >&2
exit 1
