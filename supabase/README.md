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

Use one Telnyx account, one Call Control application, one credential-based SIP connection, and one relay project
per environment. Point that Call Control application at the relay's `/functions/v1/telnyx-webhook`. Its numbers
keep that incoming route. Set its outbound voice profile and verify enough Telnyx channels for both simultaneous
calls. On the SIP credential connection set `sip_uri_calling_preference=internal` and leave its outbound voice
profile unset. Create a distinct **Telephony Credential** for each rep on that connection. The account API key
can be the same for both reps; separate keys let the operator revoke them independently.
[Telnyx contact center guide](https://developers.telnyx.com/docs/voice/webrtc/use-cases/contact-center).

1. Deploy to the isolated pilot relay first. Use an approved number on a test Call Control application. Set
   `OPENDIALER_RELAY_PUBLIC_URL` if the runtime's gateway URL differs from the project's public HTTPS URL.
   Setup uses the project's default publishable key, or `OPENDIALER_RELAY_PUBLISHABLE_KEY` if explicitly set.
   Service keys stay with the operator and on the relay.
2. Create the first workspace and its application signing route using
   [provision_rep.py](../scripts/provision_rep.py) with `--bootstrap-account` and `--signing-key` (the Telnyx Ed25519
   public key). Use an already registered route instead if deliberately adopting a legacy workspace after the
   pilot. The workspace behind that route is the default for a caller with no known history. Turning that rep off
   does not turn off signing for the pool.
3. Using the administrator connection for the target relay, create a row in `relay_private.number_pools` with a
   new UUID, the account ID, and `shared_connection_id` set to the registered application. Add the first workspace
   to `number_pool_members` with its HubSpot segment ID. Add the approved numbers to `number_pool_numbers` with the
   same fixed connection ID. Leave the hosted fallback disabled until the worker is deployed.
4. For the next rep, issue a separate profile on the same application. The invitation adds its workspace and segment
   together. Each rep imports their own file in **Settings → Call connection**. A profile may carry the Telnyx account
   key and HubSpot service key; otherwise enter them in Settings. Use the private transfer already approved for setup
   files.

```sh
uv run python scripts/provision_rep.py \
  --relay-ref "$OPENDIALER_STAGING_REF" --connection "$OPENDIALER_TELNYX_APP" \
  --pool "$OPENDIALER_POOL_ID" --segment "$OPENDIALER_REP_SEGMENT" \
  --credential "$OPENDIALER_REP_CREDENTIAL" --sip-user "$OPENDIALER_REP_SIP_USER" \
  --label "Pilot rep B" --output "$OPENDIALER_PRIVATE_PROFILES/rep-b.json"
```

Set `OPENDIALER_RELAY_ADMIN_KEY` in the operator's environment for that command. Optional
`OPENDIALER_PROFILE_TELNYX_KEY` and `OPENDIALER_PROFILE_HUBSPOT_KEY` go only into the private file. No keys or profile
files belong in Git. Give each computer its own setup file; importing the same file on two computers shares one lease.

A prospect keeps the same caller ID across reps. Seed confirmed existing caller IDs and callback ownership before a
production cutover, or let the first pooled reservation offer its existing local caller ID. Cap each number at up to
90 outbound attempts in a rolling 24 hours. Internal audio legs, callbacks and transfers do not spend those attempts.
Pausing outbound leaves the inbound route and callback history intact. The relay records which rep last called from
that number when it sees a signed carrier event for a submitted call. A missed callback stays in the rep's inbox for
up to 30 days. The app opens the locally matched prospect.

## Deploy and verify

Use the CLI v3 remote target and compare the dry run with the local migration IDs. Apply the migrations before
deploying Edge code. Use these commands against the pilot relay first:

```sh
supabase db push --remote="$OPENDIALER_STAGING_REF" --dry-run --skip-vault
supabase db push --remote="$OPENDIALER_STAGING_REF" --skip-vault
supabase functions deploy telnyx-webhook managed-setup callback-fallback --remote="$OPENDIALER_STAGING_REF"
```

The production URL stays the same when the receiver is redeployed after the pilot. The functions authenticate with
the Telnyx signature, the operator invitation, or the worker token respectively, so platform JWT verification is off.

For the hosted greeting, set Edge secrets `OPENDIALER_TELNYX_CALLBACK_KEY`, `OPENDIALER_TELNYX_ACCOUNT_ID` and a
random `OPENDIALER_CALLBACK_WORKER_TOKEN` of at least 32 characters. The worker uses its account key only to check and
control the original callback. Save the matching worker token and the project HTTPS function URL in Vault as
`opendialer_callback_worker_token` and `opendialer_callback_worker_url`. Apply
[enable_callback_worker.sql](operations/enable_callback_worker.sql) on that relay as administrator. The Cron job sends
a wake only when the database has due work. Enable `number_pools.fallback_enabled` on the pilot pool after deployment.
Use the [two-rep pilot checklist](../docs/REBUILD_PLAN.md#phase-12-shared-calling-in-21). For rollback, pause pooled
outbound first. Keep the signing route and callback bindings in service while in-flight calls settle.

```sh
node --test supabase/functions/*/*.test.mjs
OPENDIALER_REQUIRE_RELAY_TESTS=1 uv run pytest -q tests/test_relay_pool.py
```

`tests/isolation.sql` and `tests/setup-retention.sql` cover legacy relay access and retention. New calls require a fresh
relay connection and one exclusive 45-second installation lease. Realtime wakes the desktop; it also polls over
HTTP. The desktop acknowledges events after saving them. Acknowledgement clears payloads; the relay keeps dedupe
and call routing records for seven days. Private control tables are available only to server roles.

## Evidence boundary

Real Supabase delivery, isolated accounts, credential refresh and imported connection settings are tested.
Authenticated Telnyx/Soniox socket access was checked without sending audio. Browser audio checks use synthetic
tracks. A purchased/owned number, real PSTN call, microphone/WebRTC hardware, Windows packaging, paid inference,
actual provider charges and measured production usage remain separate live checks. Hosted uptime and carrier audio while a laptop is offline need the live pilot. See [the current evidence](../docs/evidence/lean/managed-integration-20261029.json).
