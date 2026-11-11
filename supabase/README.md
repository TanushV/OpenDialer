# Shared webhook service

One webhook for independent local OpenDialer workspaces:

`https://wfucwxgeexzwhinrylyn.supabase.co/functions/v1/telnyx-webhook`

The `OpenDialer` project is on Free in the Agite organization. The existing AGITE project is unchanged.
This service receives signed Telnyx controls, wakes the correct local app and retains pending controls for catch-up.
Audio and transcript text never pass through it. The desktop integration ships in OpenDialer 0.6.0.

## Caller setup

Import the private per-computer JSON profile under **Call connection** in Settings. It supplies public carrier settings,
a scoped delivery login and optionally provider keys. Sessions refresh automatically. Callers do not need tunnels,
a public PC address, SIP passwords or Supabase dashboard accounts. See [the user setup guide](../docs/GETTING_STARTED.md#calling-card).

## Operator provisioning

1. Create a dedicated Voice API application and incoming-only browser credential for each independent workspace.
   Record the application/credential connection IDs, account ID, Ed25519 public key, telephony credential ID and SIP
   username. Route both applications to the common webhook. Do not assign one application to two workspaces.
2. Generate a UUID workspace ID and a cryptographically random 36-byte URL-safe invitation. Store its SHA-256 hex
   hash in `relay_invitations` using an administrator connection, with label, public `settings` and `routes`.
   `settings` contains `telnyx_connection_id`, `telnyx_public_key`, `telephony_credential_id`, `sip_username`.
   `routes` is an array of `{connection_id, account_id, public_key}`. Never store provider API keys in this table.
3. Privately POST `{"token":"<invitation>"}` to `/functions/v1/managed-setup`. Save the response directly as a private
   JSON file. The function uses Supabase's Auth admin API and trusted invitation metadata; recipients cannot choose
   their own carrier routes. Retries reuse the same identity. Invitations expire after one hour and are cleaned up.
4. Optionally add `telnyx_api_key` and `openrouter_api_key` to that private file. Transfer it privately to its caller.
   The Auth identity is provisioned behind the scenes; no email verification or dashboard login is needed.
5. Assigned numbers appear after a signed inbound callback to a registered application. A DID already assigned to
   another workspace is refused. Route/number moves require deliberate operator reassignment with both callers idle.

To revoke access, disable the workspace and revoke its Auth sessions. To move a workspace, first stop the old
computer and reconcile calls, then restore its local data plus private managed connection/session files. Releasing
`store_id` and lease fields is an operator action, never an automatic workaround for an ownership conflict.

## Shared number pool (2.1)

Use two distinct 2.1 setup profiles with dedicated Telnyx applications and audio credentials. Apply the pool
migration in staging and verify that each application can display the same account-owned caller ID while the
number keeps its incoming route. The incoming application and its signature key must already have a
`relay_routes` entry. Use its workspace as the default destination for an unknown caller.

As the relay operator, add a row to `relay_private.number_pools` with the account ID. Add one
`number_pool_members` row per workspace, using a distinct HubSpot segment ID for each rep. Add each existing
number to `number_pool_numbers` with its fixed incoming connection ID. Set a smaller `daily_limit` for any
number still ramping. The default and maximum are 90 outbound attempts in a rolling 24 hours. Pause outbound
with `outbound_enabled=false` so old return calls still reach the rep. Keep the number and its incoming route
registered until old callbacks have aged out.

The authenticated exchange supplies only the current profile's pool and segment. A rep's first successful
reservation binds that prospect to a number. For a migration with existing bindings, seed the pool's bindings
using confirmed assignments, or let each desktop offer its existing number on its first shared reservation.
The relay records callback ownership when it sees a signed event for the submitted call. Ownership lasts
with the binding, and both the inbound call and its transfer leg stay on that desktop. The desktop handles
an active call and the greeting. The relay keeps a matched missed-call receipt for up to 30 days so an offline
desktop can record it after reconnecting.

To check the second computer's audio, start a Test call in Settings to its approved test line. Call the
displayed number back and answer on that computer. Test a paused desktop, a hidden window and a number at
its outbound cap before making the pool available to live lists.

## Deploy and verify

Apply migrations in order. Deploy `telnyx-webhook` and `managed-setup` with platform JWT verification disabled:
they authenticate with Telnyx signatures and operator-issued invitations respectively. Both use the server-provided
service-role environment variable; never export it into a caller profile. Set the publishable key in the setup
function to the target project's public key when deploying another project.

```sh
node --test supabase/functions/*/*.test.mjs
```

`tests/isolation.sql` describes the earlier disposable two-user fixture. `tests/setup-retention.sql` runs rollback-only
assertions against the provisioned Mac workspace. Neither creates production Auth users through SQL.

New calls require a fresh private-channel probe, drained local controls and an exclusive 45-second lease. Probes
and idle catch-up use RPC every 20 seconds, not Edge Function polling. After an unclean restart another consumer
may wait for the old lease to expire. Pending payloads expire after 24 hours with a gap marker; acknowledgement
immediately clears payloads, and dedupe/call mapping records expire after seven days. Hourly Cron performs cleanup.
The service-only tables have no client RLS policies or grants intentionally. The generic leaked-password advisory
remains at the Free project's default; profile passwords are generated random credentials, not human passwords.

## Evidence boundary

Real Supabase delivery, isolated accounts, credential refresh and imported connection settings are tested.
Authenticated Telnyx/Soniox socket access was checked without sending audio. Browser audio checks use synthetic
tracks. A purchased/owned number, real PSTN call, microphone/WebRTC hardware, Windows packaging, paid inference,
actual provider charges and measured production usage remain separate live checks. Do not promise free-tier uptime
or offline laptop call handling. See [the current evidence](../docs/evidence/lean/managed-integration-20261029.json).
