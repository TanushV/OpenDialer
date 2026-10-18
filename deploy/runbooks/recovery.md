# Backup, recovery, and incident handling

Use Settings → Operations → Create encrypted backup. Copy the encrypted backup plus `suppression-journal.enc` and `authorization-journal.enc` to a protected off-host destination at least daily; journals should be copied more frequently for the desired recovery point. Keep the persistent authority secret and encrypted provider vault in a separate access-controlled recovery backup. An application backup includes workspace identities and human notes but excludes plaintext provider secrets, raw media, worker leases and active claims.

Before a restore, pause reps and reconcile active carrier legs/orders. Restore via Settings or `uv run python deploy/backup/restore.py https://your-host BACKUP_ID`; the script securely prompts for an existing administrator session token. Restore authenticates the backup, retains current audit history, replays newer suppression journal events, invalidates claims and pauses reps. It sets a hard restored-authority hold.

Copy the newest off-host journals before reconciling. Review each post-backup authorization intent against signed carrier receipts/CDRs and terminal states. POST `/v1/admin/restore/reconcile` with a review reference and, when needed, a mapping of call IDs to observed provider IDs. Unresolved submissions retain the hold. Recheck preflight and explicitly go ready only after reconciliation. A restore cannot erase an opt-out; there is no ordinary rep unsuppress control.

If a carrier response is lost, preserve the original deterministic intent. Wait for signed events or inspect the existing provider resource. Never create a replacement call/order under a new random key just to clear uncertainty. Agent-leg uncertainty is also retained rather than creating another live leg. A provider-side terminal receipt permits recovery; absence of a receipt is unresolved.

For desktop or VM failure, preserve app data and restart the same installed identity. Carrier time limits bound abandoned legs while the authority is down. The recovery sweep reconciles commands/active calls; voicemail watchdogs keep detached obligations independent of the rep. New calls pause during authority loss; cached scripts/notes remain viewable and pending local opt-outs block further outbound work until acknowledged.

Test restoring a dedicated synthetic workspace before production and record checksum, recovery point, reconciled calls/opt-outs and elapsed recovery time. Do not test against live data or remove user application stores as cleanup.

## Previously submitted number orders

The no-subscription policy blocks every new number purchase, including retries with a different key. In Numbers → Previously submitted orders, reopen the original intent. If its response was lost before an order ID was saved, find that ID in the carrier account and enter it on the same row. Reconciliation only retrieves `/v2/number_orders/{id}`; it checks the ID and complete requested number set before recording fulfillment. A missing or mismatched receipt remains unresolved. Do not create another order to clear it.

Partial fulfillment lists waiting and failed numbers separately. Confirmed numbers enter unconfigured inventory once; use Sync existing Telnyx numbers to obtain their actual inventory IDs, then Configure voice route. An order-item ID is not a callable inventory ID. A timed-out configuration is reconciled by reading the existing number before another write. Changed inventory invalidates stale completion and callback evidence. Complete registration and a real authorized callback check before promotion to limited use; a successful configuration response does not prove callbacks work.

Order failure is not a billing credit. The original estimated commitment stays visible as unreconciled until account billing evidence settles it. Outbound pauses and missing inventory never automatically release phone numbers or existing callback identities. No subscription, paid reputation enrollment or new rental is activated by these recovery controls.
