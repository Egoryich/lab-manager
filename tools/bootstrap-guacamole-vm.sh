#!/usr/bin/env bash
# Install only the gateway VM's container runtime and Tailscale client.
set -euo pipefail
umask 077

if [ "$(id -u)" -ne 0 ] || [ "$#" -ne 0 ]; then
    echo 'Run as root inside the dedicated Guacamole VM, without arguments' >&2
    exit 2
fi

# Never run this on Proxmox or on the VPS.
. /etc/os-release
test "$ID" = debian && test "$VERSION_ID" = 13
test "$(dpkg --print-architecture)" = amd64
test "$(hostname -s)" = lab-guacamole

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends ca-certificates curl

install -d -o root -g root -m 0755 /etc/apt/keyrings
if [ ! -s /etc/apt/keyrings/docker.asc ]; then
    curl --fail --silent --show-error --location --retry 3 \
        --proto '=https' --proto-redir '=https' \
        https://download.docker.com/linux/debian/gpg \
        -o /etc/apt/keyrings/docker.asc
fi
chmod 0644 /etc/apt/keyrings/docker.asc
cat > /etc/apt/sources.list.d/docker.sources <<'EOF'
Types: deb
URIs: https://download.docker.com/linux/debian
Suites: trixie
Components: stable
Architectures: amd64
Signed-By: /etc/apt/keyrings/docker.asc
EOF
chmod 0644 /etc/apt/sources.list.d/docker.sources

if [ ! -s /usr/share/keyrings/tailscale-archive-keyring.gpg ]; then
    curl --fail --silent --show-error --location --retry 3 \
        --proto '=https' --proto-redir '=https' \
        https://pkgs.tailscale.com/stable/debian/trixie.noarmor.gpg \
        -o /usr/share/keyrings/tailscale-archive-keyring.gpg
fi
chmod 0644 /usr/share/keyrings/tailscale-archive-keyring.gpg
curl --fail --silent --show-error --location --retry 3 \
    --proto '=https' --proto-redir '=https' \
    https://pkgs.tailscale.com/stable/debian/trixie.tailscale-keyring.list \
    -o /etc/apt/sources.list.d/tailscale.list
chmod 0644 /etc/apt/sources.list.d/tailscale.list

if dpkg-query -W -f='${Status}' docker.io 2>/dev/null | grep -Fq 'install ok installed'; then
    echo 'docker.io is already installed; inspect package ownership before switching repositories' >&2
    exit 1
fi

apt-get update -qq
apt-get install -y --no-install-recommends \
    docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin tailscale
systemctl enable --now docker.service tailscaled.service
docker compose version
tailscale version
echo 'PASS: Docker Compose and Tailscale installed in the Guacamole VM'
echo 'Headscale enrollment and Guacamole deployment are separate steps.'
