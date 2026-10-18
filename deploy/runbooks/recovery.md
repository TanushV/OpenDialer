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


## Deferred preparation and prepaid screening

Intelligence → Deferred script preparation reads saved records without charging. Recover an uncertain original batch by its original provider ID. New batches must prove their original request metadata; legacy requests require the saved input-file context. An unrelated provider ID cannot be used to clear uncertainty. Do not submit another bulk batch to bypass the warning. A finished batch's publication is retained; only failed/missing leads are offered for explicitly budgeted new preparation. Publishing novel results may require a budgeted meaning review. None of this sends raw call transcripts to a deferred provider.

Settings → prepaid DNC status similarly reads without spending. An uncertain lookup has no provider history endpoint in the selected adapter; another query is an explicit additional charge choice. Reuse its retry identity after a lost response. A successful new lookup does not erase the previous attempt's possible cost, and paying for a query does not establish seller source authorization.

## Provider costs, partial statements and credits

In Settings → Operations → Provider cost reconciliation, review unresolved requests or switch to settled costs. This is local recordkeeping: it does not fetch an invoice, call a prospect, buy credits or initiate a subscription. Compare the original local request, provider and provider request reference with the actual statement. Enter its stable receipt ID, source reference and actual USD amount; explicitly confirm the match. A zero amount needs actual no-charge evidence. Retry a lost response with the same receipt ID and values. Different amounts or attribution require investigation, not a replacement ID. Active reservations cannot be manually released as uncertain charges.

Import carrier statements as CSV or a JSON array, at most 1 MiB/1,000 records per file. Required fields are `provider_record_id`, exactly one of `call_id`/`callback_id`, numeric `cost_usd` and integer `duration_seconds`. Optional fields are `leg_id`, `kind` (defaults to `voice`) and `request_key`; the original request key and matching kind are required to settle a reserved callback action. Map vendor records to the actual OpenDialer call identities before import. Use one record for each actual provider leg/service charge, preserving its units. Repeating a file is idempotent; conflicting rows reject the whole import.

One CDR does not prove complete call billing. The call estimate remains until the call is terminal and the administrator explicitly confirms that the listed receipts cover all legs/tracks/invocations. The remaining conservative estimate may overlap already imported charges, as stated in the UI. If the provider confirms no charge and no CDR exists, use the separate no-charge confirmation. A newly discovered record after completion reopens review. Legacy versions that cleared the entire estimate on one record are reopened once during authenticated bounded backfill, preserving the call/record IDs and audit history.

For an actual provider credit, select the settled original charge and record its own stable credit receipt and amount. Gross charge stays intact; cumulative credit cannot exceed it. The net cost changes the original charge's budget period, so an old refund cannot create additional daily spending allowance today. Original statements and adjustments remain in encrypted audit records; no invoice is independently verified by these operator-import controls. Neither a failed provider operation nor missing credentials establishes a refund.


## Workspace totals and older records

Workspace refresh reads a bounded preview. Use the explicit load-more controls for older leads, calls, follow-ups, numbers, jobs, models, trackers and registry snapshots. Lead substring search examines at most 200 encrypted records per request; an empty chunk with “More records remain to search” is not a no-match result. Continue until the cursor is exhausted. Counts describe the scoped inventory, not an uncomputed search-match total. “Prepare displayed lead packets” uses only the displayed cohort; scheduled advance preparation uses the separately configured target.

Open tasks and active/unfinished calls are included independently of recent completed history, within the displayed bounds. Use the open/overdue filter and continuation for additional obligations. Saved home metrics show their original calculation time and pending/stale/recovery status; the existing background worker refreshes them without running model fitting or capacity planning in an ordinary workspace GET. A changed source requeues this read-only work without treating it as a provider failure. Full preflight remains Settings → Operations → Run preflight. Workspace metadata alone is not a current database/provider readiness certificate; actual dialing still checks authorization at submission.
