# Backup, recovery, and incident handling

Use Settings → Operations → Create encrypted backup. Copy the encrypted backup plus `suppression-journal.enc` and `authorization-journal.enc` to a protected off-host destination at least daily; journals should be copied more frequently for the desired recovery point. Keep the persistent authority secret and encrypted provider vault in a separate access-controlled recovery backup. An application backup includes workspace identities and human notes but excludes plaintext provider secrets, raw media, worker leases and active claims.

Before a restore, pause reps and reconcile active carrier legs/orders. Restore via Settings or `uv run python deploy/backup/restore.py https://your-host BACKUP_ID`; the script securely prompts for an existing administrator session token. Restore authenticates the backup, retains current audit history, replays newer suppression journal events, invalidates claims and pauses reps. It sets a hard restored-authority hold.

Copy the newest off-host journals before reconciling. Review each post-backup authorization intent against signed carrier receipts/CDRs and terminal states. POST `/v1/admin/restore/reconcile` with a review reference and, when needed, a mapping of call IDs to observed provider IDs. Unresolved submissions retain the hold. Recheck preflight and explicitly go ready only after reconciliation. A restore cannot erase an opt-out; there is no ordinary rep unsuppress control.

If a carrier response is lost, preserve the original deterministic intent. Wait for signed events or inspect the existing provider resource. Never create a replacement call/order under a new random key just to clear uncertainty. Agent-leg uncertainty is also retained rather than creating another live leg. A provider-side terminal receipt permits recovery; absence of a receipt is unresolved.

For desktop or VM failure, preserve app data and restart the same installed identity. Carrier time limits bound abandoned legs while the authority is down. The recovery sweep reconciles commands/active calls; voicemail watchdogs keep detached obligations independent of the rep. New calls pause during authority loss; cached scripts/notes remain viewable and pending local opt-outs block further outbound work until acknowledged.

Test restoring a dedicated synthetic workspace before production and record checksum, recovery point, reconciled calls/opt-outs and elapsed recovery time. Do not test against live data or remove user application stores as cleanup.
