Found **four reachable defects**. This was a read-only source review; all cited tests were inspected, not executed.

1. **P1 — A delayed hangup response can replace the current call and disconnect its audio.**  
   [DialerPage.tsx:331](../../../apps/desktop/src/components/DialerPage.tsx:331)

   Issue two hangups for call A, holding the first response after backend completion. Let the second complete, finalize A, then start another call. Releasing the first response unconditionally restores A through `setCall`. Its subsequent parameterless `emergencyHangup()` selects the **currently selected** audio leg (`telephony/client.ts:252`), potentially disconnecting the new conversation.

   **Smallest tests-first correction:** Add that delayed-response regression, asserting both current-call identity and audio-leg identity. Fence hangup completion by the originating call/generation, apply version filtering, and scope audio cleanup to the captured original leg. The new helper-delay test covers a GET response, not this mutation response.

2. **P2 — Equal call versions allow withdrawn guidance to reappear.**  
   [DialerPage.tsx:87](../../../apps/desktop/src/components/DialerPage.tsx:87)

   Hold a helper-triggered `reloadCall` response containing advice. Update its knowledge source, then allow the subscribed refresh to return `coach: null`. Release the older response afterward. Both responses can have identical call and transcript versions: guidance publication changes the conversation/event records, and knowledge publication changes knowledge, without incrementing those two call fields. The `>=` checks therefore accept the obsolete advice.

   Backend `visible_help` correctly rejects changed sources (`coaching/workflows.py:377`), but cannot retract an already captured HTTP response.

   **Smallest tests-first correction:** Delay one same-call GET across knowledge invalidation and assert withdrawn advice stays absent. Share a request/invalidation generation fence across subscribed refreshes and `reloadCall`; call-version comparisons alone are insufficient.

3. **P2 — Load receipts can pass while maintenance and queued work fail internally.**  
   [measure_completion.py:876](../../../scripts/measure_completion.py:876)

   `run_load` marks background work successful whenever its future returns. Actual maintenance catches per-user failures and only logs them (`intelligence.py:1616`); actual job execution catches failures, records retry/dead state, and returns zero processed (`intelligence.py:1416`). Neither makes the future fail. Normal load never checks the resulting failed/dead/backlogged work. Shift maintenance likewise equates “returned normally” with success.

   Thus repeated preparation/reconciliation failures can produce passing maintenance/load evidence and a smaller completed workload. The new measurement test raises directly from the maintenance stub, bypassing the real swallowed-error boundary.

   **Smallest tests-first correction:** Inject failures inside real maintenance operations and job handlers, then require failed measurement receipts. Capture explicit maintenance outcomes and job failure/backlog deltas; preserve failures even if later retries succeed.

4. **P2 — The shared reference gate does not bind the manifest to the executing containers/database.**  
   [measure_completion.py:201](../../../scripts/measure_completion.py:201)

   The gate compares the executing cgroup’s CPU/memory limits with manifest values, but accepts container IDs, network ownership and database limits solely from the supplied JSON. It never verifies the executing container identity/network namespace or that the PostgreSQL connection reaches the manifest’s database container.

   A stale manifest plus another container with matching API limits and access to a different disposable PostgreSQL server can therefore pass as the combined 2-vCPU/4-GiB reference.

   **Smallest tests-first correction:** Add a counterexample with matching limits but mismatched runtime/database identities. Bind inspected container identities, runtime namespaces and the actual database endpoint before awarding reference status.

The prior reported repair paths are present in source: transactional denial journaling, source-owned meeting correction including held→booked, indexed inbound lookup, retained paused-number occupancy, logout-specific replay, forecast screening, and protected-copy SQLite migration. The migration tests cover interruption/resume, changed source, competing writers, publication rollback, directory-flush failure and cleanup. The new restore, denial-restart and followthrough assertions exercise the requested combined paths; I found no additional concrete defect there.

Native updates/process behavior, hardware audio, carrier/provider behavior, legal/source authorization, funded inference quality and actual resource targets remain separate, unvalidated gates.

Fingerprints were unchanged:

```text
Entry HEAD: 67e2120e80610915f57698de70ab6977398f8562
Exit HEAD:  67e2120e80610915f57698de70ab6977398f8562

Entry/exit production digest:
cc40520d0e98700e5e44a1696e5c15cd6a6e370fdcaa07d5c6dae4f878b0725c
```

Digest computed with `scripts.inspect_architecture.digest(source_files())`, without invoking its report writer. No files were edited or prohibited operations performed.