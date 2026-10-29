# OpenDialer webhook receiver

**Development checkpoint — not yet a usable desktop setup.**

The Free `OpenDialer` project in the existing Agite organization is deployed at:

`https://wfucwxgeexzwhinrylyn.supabase.co/functions/v1/telnyx-webhook`

It receives signed Telnyx control events for registered applications. Audio and transcript
text are not stored or broadcast. Each workspace uses its own authenticated, private
notification channel and fetches only its pending controls. Acknowledgement happens after
local durable receipt. The operator must register routes; an unregistered application gets 404.

## Verified

- Ed25519 signature/timestamp rejection, deduplication and collision handling.
- Two independent local SQLite stores receiving their own synthetic events through this URL.
- Private Realtime delivery, persisted SDK session refresh, exclusive consumer lease,
  row-level isolation and remote acknowledgement after local receipt.
- Original receipt timestamps and expired callback windows; a delayed callback becomes a
  review task rather than answering an old call.

The two service-only tables intentionally have no authenticated-client RLS policies. Clients
have no direct table privileges; the restricted RPC mediates fetch/ack. Supabase reports
these as informational notices, not permission failures. Keep those tables closed.

## Deployment

Use Supabase CLI 2.122.0 or the connected Supabase tools. Apply the migration, then deploy
`telnyx-webhook` with platform JWT verification disabled: the function authenticates the
exact request body itself using Telnyx Ed25519 signatures. Other operations remain authenticated.
The function uses its server-provided service-role environment variable. Never put that
key in the desktop or recipient profiles.

Run the receiver contracts with Node 24:

```sh
node --test supabase/functions/telnyx-webhook/receiver.test.mjs
```

`tests/isolation.sql` is a rollback-only assertion script for the two disposable signed-event
fixtures described in the verification receipt. It is not a production user-provisioning script.
Test Auth identities, routes and credentials are removed after the live service check.

## Remaining before use

Follow [the feature plan](../docs/MANAGED_WEBHOOK_PLAN.md). Direct audio capture and its
loopback bridge, real Soniox access, app startup/readiness integration, number assignments,
recipient profile provisioning/import UI, retention and gap reconciliation, packaging and
carrier/audio qualification are still open. The installed app is unchanged. Do not point a
real Telnyx application here until its recipient and local integration are ready.
