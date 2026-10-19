**E13 remains open.** Static review found five reachable software gaps. The three preceding findings have targeted fixes, but meeting correction still has an uncovered branch.

This review inspected source and maintained tests only. No files were edited; no tests, builds, services, credentials, installed app data, connectors or network requests were used. Assertions described below were read, not executed.

**Findings**

1. **P2 — Held→booked correction can retract a later independent meeting report.**

   `save_disposition` routes either meeting outcome through `_meeting_in` at [rep_workflows.py:1044](../../../services/backend/app/rep_workflows.py:1044). `_meeting_in` replaces the lead’s current meeting and retracts the lead-level conversion whenever the new status is not held, without checking its source call at [rep_workflows.py:856](../../../services/backend/app/rep_workflows.py:856).

   **Trigger:** Save call A as `meeting_held`; independently report the lead’s meeting as held; then correct A to `meeting_booked`. The independent report has `source_call_id=None`, but the correction overwrites its meeting state and retracts its conversion.

   The new source guard protects correction **away from both meeting outcomes**. `test_call_correction_cannot_retract_a_later_independent_meeting_report` at `test_local_followthrough.py:66` exercises `right_party`, leaving this branch unasserted.

   **Minimal regression:** Repeat that test with correction to `meeting_booked`; require the independent held evidence, goal state and hold to survive, including replay and restore.

   **Narrow remedy:** Apply source ownership checks to meeting-to-meeting corrections as well as nonmeeting corrections. Updating historical call evidence must not silently replace a later independent lead report.

2. **P2 — The allocator drops account occupancy belonging to an unusable number.**

   `Assignment.solve` filters inventory to usable numbers before collecting active intervals at [capacity.py:468](../../../services/backend/app/optimization/capacity.py:468). Its account occupancy loop subsequently visits only that filtered inventory at line 476.

   **Trigger:** A paused number retains a live or detached leg occupying the sole configured account channel. Another healthy number and an available rep exist. The paused number cannot originate, but its leg still consumes account capacity; the allocator omits that occupancy and can schedule another attempt.

   `inventory_capacity` preserves claims and detached leases as `active_obligations` (`optimization/workflows.py:342`), and `number_input` converts those obligations into intervals (`capacity.py:317`). The loss occurs inside assignment.

   **Minimal regression:** One paused DID with an obligation covering the staffed window, one healthy DID, one available rep, and `account_channel_limit=1`: require no overlapping assignment in existing, expanded and contingent schedules. After authoritative terminal release, require capacity to become available.

   **Narrow remedy:** Accumulate account occupancy from all retained obligations; separately restrict origination choices to usable numbers. `test_declared_account_channel_limit_applies_across_numbers` (`test_unified_planning.py:172`) currently covers usable numbers only.

   This is a planning defect; static inspection does not establish an actual carrier-limit bypass.

3. **P2 — Lost logout response leaves native authority switching unable to reconcile completion.**

   `AuthorityState::logout_once` sets `logout_confirmed` only after receiving a successful response at [lib.rs:46](../../../apps/desktop/src-tauri/src/lib.rs:46). The backend commits session revocation before returning success (`main.py:403–413`). A subsequent attempt uses that revoked token, which `decode_session` rejects (`security.py:434–449`).

   **Trigger:** Logout commits, but its response is lost. Native state retains the binding and `logout_confirmed=false`; retry receives an authentication error. `disconnect_authority` therefore cannot reach owned-resource cleanup or binding clearance. `App.disconnect` also retains its session because clearing follows the native call (`App.tsx:437–454`).

   **Minimal regression:** Commit logout, drop its response, then retry disconnect. Require an authenticated, operation-specific reconciliation result and exactly-once cleanup. Also test restart between those stages and refusal while obligations remain.

   **Narrow remedy:** Retain a replayable logout completion receipt for the original session/operation and provide narrowly scoped reconciliation. Do not treat an arbitrary 401 as proof of completed quiescence.

   `successful_logout_is_not_repeated_when_native_cleanup_needs_retry` (`lib.rs:831`) covers a **received** successful response followed by cleanup retry, not response loss.

4. **P2 — Unified forecast treats missing national screening as executable demand.**

   `forecast_input` enriches leads with internal suppression, binding and active-claim state at [workflows.py:604](../../../services/backend/app/optimization/workflows.py:604), but does not obtain national screening eligibility. `work_items` (`capacity.py:147`) admits otherwise eligible leads without current national coverage. The resulting shortfall categories contain staffing, binding, setup and number constraints, with no screening constraint (`capacity.py:1339`).

   **Trigger:** Retain an eligible US lead whose authorized national source/cache is missing, stale or outside its NPA coverage. The forecast can schedule it and attribute shortages to numbers, while actual authorization correctly blocks it.

   **Minimal regression:** Remove national publications and exact prepaid clearance, then compare readiness, authorization and forecast for the same lead. Require explicit blocked/pending screening demand and no representation of that work as currently executable. Repeat after restoring an expired or uncovered cache, without issuing a paid lookup.

   **Narrow remedy:** Carry the shared screening decision into planner eligibility as a hard constraint, preserving blocked leads in the original denominator. Conditional future screening may be reported explicitly, but must not appear as current executable capacity.

   `test_prepaid_preflight_authorization_agree` (`test_screening_readiness.py:11`) checks readiness, authorization and queue selection; it does not assert forecast agreement.

5. **P2 — Migration can modify the input store before discovering later corruption.**

   `Database.__init__` performs schema changes before authentication (`db.py:338–343`). `encrypt_legacy_records` authenticates only one existing row per document table before legacy rewrites (`db.py:350–354`). The later sanitation pass authenticates pages while committing their changes and cursor at [privacy.py:780](../../../services/backend/app/privacy.py:780).

   **Trigger:** An older store contains valid early pages requiring sanitation and a corrupt encrypted row in a later page. Earlier changes commit before the later authentication failure. Startup refusal therefore does not imply preservation of the original input store.

   **Minimal regression:** Corrupt a nonfirst encrypted row after a sanitation page boundary; attempt migration; require refusal and unchanged original schema/business payloads. Repeat with a wrong key and interruption before publication.

   **Narrow remedy:** Perform authentication and migration on the protected working copy, publishing it only after validation succeeds. Retain resumable sanitation on that copy.

   `test_database_encryption_key_is_required_and_wrong_key_cannot_decode` (`test_encrypted_storage.py:42`) asserts rejection only. `test_sanitation_commits_bounded_pages_and_resumes` (`test_installed_copy_migration.py:78`) asserts resumability, not original-store preservation on corruption.

**Trace of the preceding fixes and remaining paths**

| Path | Source and inspected assertions | Static conclusion / missing assertion |
|---|---|---|
| Disposition suppression | `save_disposition`, `rep_workflows.py:945`; `suppressions.record`, `compliance/suppressions.py:155`; `apply_in`, line 49. New regressions: `test_suppression_durability.py:13` and `:40`. | Suppression journaling begins inside the disposition transaction before wrap-up mutation/receipt. Rollback retains denial recovery ownership; restart replays the journal. Repeated requests retain identity. Correcting the disposition does not implicitly cancel an opt-out. No additional defect found in this fix. The new restart assertion checks suppression presence; it does not itself exercise both reservation and submission after restart. |
| Meeting→nonmeeting correction | `rep_workflows.py:920–943`, `:1052–1074`; `test_local_followthrough.py:15`, `:66`, `:99`. | Matching conversion is retracted; matching meeting becomes `needs_review` with an outreach hold; unrelated later report survives this branch. Tests inspect current totals, training labels and historical as-of conversion. Explicit cancellation/resume clears the hold. Finding 1 covers the remaining meeting-to-meeting branch. |
| Indexed inbound identity | `InboundCallbacks.inbound_event`, `telephony/callbacks.py:314`; `operational_schema.py:226`; migration `0017_metric_projections.py:62`. New tests: `test_inbound_completion.py:16`, `:57`. | Exact tenant/provider query loads at most two matches; client-state fallback uses an exact task ID and validates routing-attempt membership. Existing assertions cover forged metadata (`:323`), caller cancellation/late answer (`:302`), uncertain answer (`:189`), lost ring receipt (`:311`) and restart reconciliation (`:436`). No additional defect found in this lookup fix. Duplicate-provider ambiguity has an explicit rejection branch; the new bounded-read tests do not directly assert it. |
| Conversation restore → interpretation → correction | Backup/merge: `administration.py:732`, `:795`; retained source tables: `db.py:184`; lineage loading/storage: `coaching/workflows.py:35`, `:43`; inference context: `intelligence.py:1750`; publication fences: `:1694`, `:2090`; correction invalidation: `privacy.py:624`, `coaching/context.py:907`. | Restore retains conversation state, lineage metadata and original versions, keeps current rows, and clears raw memory. New interpretation reloads durable lineage; correction traverses dependent observation versions. Finalization cancellation uses shielded completion (`intelligence.py:1939`); expired owners cannot publish. **Missing combined executable assertion:** restore into a separate authority, interpret a new chunk, then correct/retract an earlier retained source; assert dependent invalidation, preserved human notes/tasks, no raw resurrection and rejection of an old in-flight response. Existing TTL/correction tests (`test_conversation_finalization.py:595`, `:681`, `:882`) do not perform restore. |
| Review correction UI | `ReviewPage.tsx:38`, `:194`; `WrapUp.tsx:16`, `:37`; `ProspectBrief.tsx:24`, `:271`, `:315`; paired browser journey `followthrough.spec.ts:5`. | Review reloads are selected-call/generation fenced; wrap-up initializes confirmed values and reuses a request identity while the submitted fingerprint stays unchanged. `needs_review` prevents saving until a corrected status and required outreach decision are selected. **Missing assertions:** committed disposition with lost response followed by retry; late save/reload after navigating to another call; stale-version correction followed by reload and resubmission. |
| Small window, focus and call-source changes | `CallControls.tsx:51–77`; `LiveCoach.tsx:29–42`, `:110–140`; `DialerPage.tsx:102–148`; `completion.spec.ts:300–375`. | Call-ID changes reset helper text/expansion and mute/keypad UI; pending helper cleanup targets its original call; stale call-refresh responses are disposed. Browser assertions inspect script/helper/hangup visibility, F1, Escape and outage controls. **Missing assertion:** actual focus retention through an asynchronous update—the test explicitly refocuses the question at line 343 before continuing. Also missing: source/call change while a help request is pending, followed by its late response. |
| Allocator success, correction and limits | `capacity.py`: availability `:230`, occupancy `:463`, exact/greedy search `:659`, additions `:907`, contingent retries `:995`, changed-page planning `:1114`, full forecast `:1165`. Tests include independent oracle `test_unified_planning.py:85`, conservation `:45`, duration `:143`, global budget `:194`, source publication `:673`, retry ledgers `:883`. | General capacity is shared; stable bindings, owner windows, durations, setup and bounded search are represented. Truncated search does not certify a minimum. No external purchase is authorized. Findings 2 and 4 remain; existing oracle coverage does not include those states. |
| Restored authorized DNC cache | Restore merge `administration.py:839–920`; exact cache lookup and authorization stamp `compliance/operating.py:455–525`; source decision `:299`; chunk identity/checksum validation `compliance/registry.py:34`. | Restored cache content alone cannot authorize: current seller authorization, exact phone, stamp, coverage, freshness and nonsynthetic status are rechecked. Missing coverage remains blocked in actual authorization. Tests inspect changed authorization (`test_screening_readiness.py:73`), missing coverage (`:11`, `:94`), original expiry (`test_dnc_recovery.py:279`, `:305`), cancellation (`:443`) and lost-response liability (`:153`). **Missing combined assertion:** restore cache/chunks into a separate authority, then exercise authorization and submission with valid, expired, changed-authority and uncovered-NPA cases, asserting zero automatic paid refreshes. |
| Native switch/update ownership | Binding guards `lib.rs:179`; disconnect `:367`; owned exit cleanup `:1149`, `:1540`; updater quiescence/pending-opt-out checks `SettingsPage.tsx:1397`; backend logout quiescence `main.py:403`. | Source checks selected authority, pending opt-outs and backend obligations; cleanup targets owned resources. Existing native tests inspect binding guards, tunnel ownership and received-logout cleanup retry. Finding 3 remains. Static source does not prove actual updater process exit or installed backend replacement. |

**Evidence boundaries**

Native Mac/Windows execution, signed in-place update continuity, actual process termination, hardware audio/sleep behavior and true OS publisher trust were not validated. Neither were carrier behavior, account eligibility, seller/source legal authorization, provider privacy controls, funded Cerebras routing, human semantic quality, outcome lift or actual latency/resource targets. These require separate results; their absence is not itself a missing-software finding.

Entry and exit fingerprints match:

```text
Entry HEAD: 88a0fa5f5bb612601e5ab7fac5b087356f809fc6
Exit HEAD:  88a0fa5f5bb612601e5ab7fac5b087356f809fc6

Entry production digest:
0fefab4ddce008ea5557f7ebc3836f9fd2a6b5c2ff9fe88b10004c8346c780d3
Exit production digest:
0fefab4ddce008ea5557f7ebc3836f9fd2a6b5c2ff9fe88b10004c8346c780d3
```

Both digests used `scripts.inspect_architecture.digest(source_files())`; the report writer was not invoked. No production-source change was detected between fingerprints. The checkout remained dirty with the same listed changes.
