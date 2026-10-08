# Guacamole gateway

The dedicated Debian 13 VM is prepared on Proxmox first. Inside that VM,
[`bootstrap-guacamole-vm.sh`](../../tools/bootstrap-guacamole-vm.sh) installs
Docker Engine, Compose and Tailscale from their official Debian 13 apt
repositories. It checks the VM hostname before changing apt sources and does
not join Headscale or start Guacamole. Run it as root only after the VM has a
tested, restricted Internet route. [Docker's Debian installation guide](https://docs.docker.com/engine/install/debian/)
and [Tailscale's Debian packages](https://pkgs.tailscale.com/stable/debian/)
describe those repositories.

The initial Proxmox-side check succeeded with a running VM, key-only SSH,
completed cloud-init, DNS resolution and a Debian package-index refresh. For
another installation, substitute its VMID, private VM address and private key;
run each short line separately so terminal paste cannot truncate a heredoc:

```bash
qm status '<GUACAMOLE_VMID>'
ssh -i '<GUACAMOLE_BOOTSTRAP_KEY>' -o BatchMode=yes '<VM_USER>@<VM_PRIVATE_IP>' 'cloud-init status --long; getent ahostsv4 deb.debian.org'
ssh -i '<GUACAMOLE_BOOTSTRAP_KEY>' -o BatchMode=yes '<VM_USER>@<VM_PRIVATE_IP>' 'sudo -n apt-get update -qq && echo PASS-APT'
```

The cloud image may report `degraded done` solely because Proxmox emits the
deprecated `user` cloud-init field. Check `errors: []` and the actual network
tests before treating that warning as a provisioning failure. Never rerun the
VM creation script against a VMID that already exists.

The Debian 13 gateway bootstrap was also completed successfully: the pinned
script checksum matched, the VM installed Docker Engine, Compose and Tailscale,
and printed `PASS: Docker Compose and Tailscale installed in the Guacamole VM`.
Run the same script for a new gateway only after the three checks above. Fetch
it by an immutable Git commit and verify its SHA-256 before sending it over
SSH to `sudo -n bash -s` inside that VM. Keep the script file on Proxmox for
inspection and execute each short command separately; never pipe an unchecked
network response straight into a shell.

The gateway has since joined Headscale. From the Proxmox host, the following
read-only check returned the gateway's tailnet IPv4 and listed the VPS and
Proxmox peers. Substitute the local bootstrap key, user and private VM address
on another installation; do not save an authentication URL or preauth key in
this guide:

```bash
ssh -i '<GUACAMOLE_BOOTSTRAP_KEY>' '<VM_USER>@<VM_PRIVATE_IP>' \
  'tailscale ip -4; tailscale status'
```

The health warning that some peers advertise routes while `--accept-routes` is
false is expected for this VM: it does not use tailnet subnet routes. Check
reachability from the VPS separately before starting the private Guacamole
stack:

```bash
tailscale ping '<GUACAMOLE_TAILNET_IP>'
```

The VPS check succeeded for the registered gateway: replies first used a relay
and then a direct peer path. This verifies tailnet reachability, but not yet
the Guacamole HTTP service or a connection to a student guest.

This stack runs inside a dedicated infrastructure VM on Proxmox, not on the
small VPS or the Proxmox host. It uses the official Guacamole 1.6.0 and guacd
image digests. Only the VM's tailnet IPv4 binds port 8080; guacd has no
published port. The VPS Caddy proxy will expose `/guacamole/` under the Lab
Manager HTTPS origin after the VM's address and routing have been verified.

The current Compose file uses Guacamole's encrypted JSON authentication only
for isolated gateway/protocol validation. It is **not** the student-facing
authentication design. The VPS API now records a one-time browser launch and
sets a short-lived HttpOnly cookie scoped to `/guacamole`; it never returns the
launch nonce in a URL or JSON body. A pinned Guacamole authentication extension
must consume that nonce, recheck the active run and close tunnels on revocation
before the public proxy and student access are enabled. The stock JSON
extension does not provide those guarantees.

The disabled-by-default VPS gateway endpoint is
`POST /api/internal/guacamole/consume`. The gateway sends the 43-character
nonce in a JSON body and authenticates the request with
`X-Lab-Guacamole-Time` (Unix seconds) and
`X-Lab-Guacamole-Signature` (hex HMAC-SHA256 of
`<seconds>:<nonce>`). The VPS accepts at most 30 seconds of clock skew,
atomically marks the nonce consumed, rechecks the live lesson, session,
membership, runtime, network and reservation, then returns only that SSH
connection. The 32-byte shared key belongs only in the gateway and VPS secret
stores. A successful exchange does not by itself implement active tunnel
revocation, and the public proxy remains disabled until the authentication
extension and lifecycle checks are in place.

For the isolated JSON-auth check, generate a random 16-byte key with
`openssl rand -hex 16`; do not commit or log it. The value is local to this
test stack and must not be used as a substitute for the broker exchange.

Create a root-readable `.env` beside `compose.yml` containing `GUAC_BIND_IP`
and `JSON_SECRET_KEY`. `GUAC_BIND_IP` must be the tailnet IPv4 of this VM.
Keep port 8080 and guacd off public and student-facing interfaces. Run
`docker compose --env-file .env -f compose.yml config --quiet` before starting
and verify the bind address with `docker compose ps` and `ss -lntp`.

Guacd must reach only assigned guest SSH addresses. The route and firewall
allowance from this VM to active lab subnets are separate steps; until they are
verified, no guest connection should be reported ready. A JSON grant's
`expires` limits new logins but does not revoke an existing session. Lesson
stop must close active sessions and verify the guest is stopped before
releasing compute.

References: [Apache Guacamole Docker deployment](https://guacamole.apache.org/doc/gug/guacamole-docker.html),
[JSON authentication](https://guacamole.apache.org/doc/gug/json-auth.html),
[SSH connection parameters](https://guacamole.apache.org/doc/gug/configuring-guacamole.html).
