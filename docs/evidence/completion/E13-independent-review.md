**E13 should remain open.** This read-only review found three reachable software defects. Tests cited below were inspected, not executed.

1. **P1 — Wrap-up opt-out commits before durable suppression.**
   [rep_workflows.py:1080](../../../services/backend/app/rep_workflows.py:1080), `save_disposition`.

   **Trigger:** Save an `opt_out` disposition; interrupt execution after the disposition transaction commits but before `self.suppress()` starts. The call, outcome and edit receipt survive, but no suppression journal entry or suppression row exists. After restart, normal authorization checks suppression/holds rather than `last_outcome`, so subsequent otherwise-eligible contact remains possible.

   **Impact:** A recorded opt-out can lack its authoritative denial. There is also an inter-transaction authorization window.

   **Minimal regression:** Interrupt at entry to `suppress`, restart the authority, advance to the next permitted attempt window, and assert that both reservation and submission remain blocked. Existing `test_remediation_privacy.py` asserts suppression after an uninterrupted disposition; `test_suppression_durability.py` covers failures after suppression journaling begins. Neither covers this preceding gap.

2. **P2 — Correcting a meeting disposition leaves its derived conversion and hold active.**
   [rep_workflows.py:1011](../../../services/backend/app/rep_workflows.py:1011), `save_disposition`; [rep_workflows.py:822](../../../services/backend/app/rep_workflows.py:822), `_meeting_in`.

   **Trigger:** Save `meeting_held`, then correct that same call to `right_party`. The first save creates the `rep-meeting:<lead>` conversion event and meeting hold. The correction updates the call but skips `_meeting_in`, leaving both behind. `goal_state`, `lead_values` and `training_rows` still accept the unretracted event.

   **Impact:** The corrected call can remain a reported conversion and training positive, while outreach stays held.

   **Minimal regression:** Perform that sequence with valid request IDs/versions; assert the disposition-derived meeting evidence is retracted, current conversion totals/labels reverse, historical as-of results remain intact, and any required outreach decision is explicit. Existing meeting tests assert holds, explicit cancellation actions and version conflicts, but not this correction across the two representations.

3. **P2 — Every ordinary signed call event can decrypt the complete task history.**
   [callbacks.py:316](../../../services/backend/app/telephony/callbacks.py:316), `inbound_event`.

   **Trigger:** Accumulate completed follow-up tasks, then process an ordinary outbound webhook. `_apply_webhook_in` calls `inbound_event`, which loads all tenant `followup_tasks` through `rows()` before filtering for inbound records.

   **Impact:** Event-processing work grows with historical tasks, including unrelated sellers’ tasks within the tenant. This violates the required bounded hot-read mechanism; no latency threshold failure is claimed from this static inspection.

   **Minimal regression:** Seed thousands of terminal/unrelated tasks, process one outbound event and one matched inbound event, and assert bounded task decryptions plus correct routing. Existing inbound assertions cover routing, deduplication, fallback and terminal handling, not this history-growth case.

The production digest stayed unchanged during review:

```text
Entry HEAD: 6b98b621d2e30e68c5accdda5a81a64ad8b85f0b
Exit HEAD:  88a0fa5f5bb612601e5ab7fac5b087356f809fc6
Production digest:
af44ee8f62f6120c39aeff4c9346f73c3960f72b6c3eb36e4d80ffe7a09a9ed8
```

Computed using `scripts.inspect_architecture.digest(source_files())`; the report writer was not invoked. The implementing tree was committed during inspection, including the rep-day repair.

| Domain | Success / correction / cancellation inspected | Crash / restore coverage and limits |
|---|---|---|
| Telephony, callbacks, accounting | `accept_webhook` → `_process_webhook` → reducer; correction rollback; terminal callback cleanup; `usage` settlement and unsent release. Existing assertions check one receipt/job, atomic rollback, one processed event, fallback hangup and retained uncertain review cost. | Webhook completion fault/retry and operation recovery inspected. Restore assertions preserve submitted calls, claims, terminal commands and settled credits. Finding 3 remains. |
| Conversation, review, expiry | `_validated_help`, `_review_help`, `_publish_help`; changed-source rejection, factual-stream withholding and canceled review accounting. Long-call assertions retain early derived context after TTL and preserve human notes on correction. | Expired finalizer ownership inspected. Generic restore preserves retained notes; conversation-specific restore followed by new inference was **not traced**. |
| Preparation, tasks, meetings, UI | Preparation leases/source checks; task replay, snooze/cancel and packet refresh; `WrapUp`; complete journey assertions preserve one task through restart and require explicit no-show action. | Cancellation releases preparation leases; deferred recovery retains original batch identity. Restore assertions preserve later tasks/notes/holds. Finding 2 remains. Full small-window/focus behavior was **not independently checked**. |
| DNC, identity, capacity, health | Shared `screening_capability`; source-change invalidation; callback-proof expiry; stable number selection. Assertions cover prepaid readiness without spending, changed-route invalidation, shared general capacity and independent small allocation oracle. | Lost configuration response reconciles without another PATCH; denial rollback/journal and uncertain-number restore assertions inspected. Full allocator internals and restored DNC-cache execution were **not traced**. Finding 1 remains. |
| Labels, features, projections | `outcome_at`, `conversion_at`, `training_rows`, `prior_features`, contribution replacement and rebuild activation. Assertions exclude future/provisional features and preserve inactive totals during interrupted rebuild. | Current rep-day repair and regression inspected: repeated inputs reuse one day contribution; later correction must yield 3,600 seconds. Restore/rebuild namespace handling inspected; no runtime validation performed. |
| Privacy, migration, backup, native authority/update | `EphemeralBuffer`, durable sanitation, backup/restore merge, authority binding, owned tunnel cleanup and sidecar launch boundary. Assertions preserve current access/business data and permit emergency stop on journal failure. | Wrong-key/corrupt-store migration paths, full native authority transitions and actual signed updates were **not completely traced**. No installed data, credentials or devices were accessed. |

These are product findings, separate from unfinished E13 coverage/load/freeze and E14–E15 delivery work. Carrier/account authorization, upstream retention, funded semantic quality, hardware behavior and publisher trust remain external evidence gates. This partial static pass does **not** establish whole-system correctness or close the independent-review gate.
