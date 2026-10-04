# Guacamole gateway

This stack runs inside a dedicated infrastructure VM on Proxmox, not on the
small VPS or the Proxmox host. It uses the official Guacamole 1.6.0 and guacd
image digests. Only the VM's tailnet IPv4 binds port 8080; guacd has no
published port. The VPS Caddy proxy will expose `/guacamole/` under the Lab
Manager HTTPS origin after the VM's address and routing have been verified.

Guacamole's official encrypted JSON authentication extension accepts a
short-lived signed grant from Lab Manager. It contains one authorized SSH
connection, or a `join` connection for teacher assistance. No Guacamole user
database or separate student password is required. The same random 16-byte
key must be configured as `JSON_SECRET_KEY` here and held only by the VPS
backend. Generate it with `openssl rand -hex 16`; do not commit or log it.

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
