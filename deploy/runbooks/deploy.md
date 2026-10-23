# Deploy one shared authority

1. Use a Linux host you already own with persistent volumes, time synchronization, restricted SSH, a firewall and an off-host backup destination. Size it for the team: start with 4 vCPU for a small team, with the background worker on its own CPU share. The 2-vCPU reference misses the latency targets even at two reps per minute (see [validation](../../docs/VALIDATION.md)). Measure your own point with `scripts/bench.py` before relying on it.
2. Copy `deploy/.env.example` to a protected path outside the checkout and set the DNS hostname, a random PostgreSQL password and a one-time bootstrap token. Never commit this file.
3. Run `docker compose --env-file /protected/opendialer.env -f deploy/compose.yml up -d --build`. The `migrate` service creates the schema on a fresh database and exits; the API and the background worker start after it. PostgreSQL has no published host port; only Caddy exposes HTTP/HTTPS. Confirm DNS/TLS and `/health`.
4. Bootstrap once with `POST /v1/setup`, the `X-Bootstrap-Token` header and the seller, campaign and admin details over HTTPS. Setup cannot initialize a second first workspace; the platform owner creates further workspaces explicitly.
5. In the desktop, connect to the HTTPS server, sign in and configure providers, policy, registries and budgets in Settings. Create users with individual incoming-only WebRTC credentials. Never share the carrier account key with rep desktops.
6. Exercise backup/restore and signed webhook events on permissioned test endpoints before admitting production traffic. Unconfigured providers and missing legal evidence fail closed.

The API process runs call control, webhook processing, live coaching and post-call work (`OPENDIALER_WORKER_QUEUES`). The background worker runs preparation, research and the statistical refresh and training. Each process keeps separate HTTP pools for call control (16), live coaching (12) and background work (4). Provider keys live in the shared data volume as plain JSON; protect the host and its backups accordingly.

Fresh stores only: this release creates its schema on an empty database and does not upgrade stores from 0.2/0.3.

## Local authority with an existing SSH host

Use this only with a hostname and SSH host you already control. The desktop starts the OS SSH client to forward signed Telnyx webhooks to the local authority.

1. On the forwarding host, dedicate an SSH user and key to this workspace, restricted to remote forwarding (`GatewayPorts no`, `PermitListen 127.0.0.1:18765`). Accept the verified host key in the rep's OS known-hosts file.
2. Reserve remote loopback port 18765 for this one authority; give another workspace a distinct port and hostname.
3. Point an owned DNS hostname at the host and run Caddy with `deploy/Caddyfile.local-ingress` (not the connected Caddyfile), setting `OPENDIALER_INGRESS_DOMAIN` and `OPENDIALER_FORWARD_PORT=18765`. It forwards only `POST /webhooks/telnyx` and `GET /webhooks/ingress-check/<nonce>`; everything else returns 404.
4. In the local workspace, open Settings → Providers → Existing SSH forwarding host, enter host, user and port, enable and save. The desktop restarts the forward when the local authority restarts.
5. Save `https://<hostname>/webhooks/telnyx` and the Telnyx public verification key in Settings, configure the same URL on the carrier application, and click **Check saved ingress**.

An offline laptop cannot route callbacks. Use the same authority in connected mode on an always-on host when callbacks must be answered while the laptop is off.
