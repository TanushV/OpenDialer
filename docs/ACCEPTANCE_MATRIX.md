# Revised acceptance evidence map

Authored before October 17 remediation production edits. This map indexes the maintained test contracts; a mapped family is not automatically passed or fully live-certified. All tests in each mapped module run in the final relevant suite. New counterexamples require a red regression before repair. Raw red logs are protected ignored work artifacts; summarized receipts and hashes are recorded in STATUS.md.

| Family | Maintained software contracts |
| --- | --- |
| A01 | [legacy bootstrap jobs](../services/backend/tests/test_bootstrap_migration.py), [test_migrations](../services/backend/tests/test_migrations.py), [test_encrypted_storage](../services/backend/tests/test_encrypted_storage.py), [test_remediation_privacy](../services/backend/tests/test_remediation_privacy.py) |
| A02 | [test_operations](../services/backend/tests/test_operations.py), [test_remediation_boundaries](../services/backend/tests/test_remediation_boundaries.py) |
| A03 | [test_connected_security](../services/backend/tests/test_connected_security.py) |
| A04 | [test_remediation_audit](../services/backend/tests/test_remediation_audit.py), [test_remediation_privacy](../services/backend/tests/test_remediation_privacy.py), [test_webhook_completion](../services/backend/tests/test_webhook_completion.py), [review warning](../apps/desktop/tests/grounding.spec.ts) |
| A05 | [test_carrier_and_recovery](../services/backend/tests/test_carrier_and_recovery.py), [test_remediation_audit](../services/backend/tests/test_remediation_audit.py), [test_webhook_completion](../services/backend/tests/test_webhook_completion.py) |
| A06 | [test_remediation_boundaries](../services/backend/tests/test_remediation_boundaries.py), [test_carrier_and_recovery](../services/backend/tests/test_carrier_and_recovery.py) |
| A07 | [test_remediation_audit](../services/backend/tests/test_remediation_audit.py), [test_remediation_privacy](../services/backend/tests/test_remediation_privacy.py) |
| A08 | [test_operations](../services/backend/tests/test_operations.py), [test_carrier_and_recovery](../services/backend/tests/test_carrier_and_recovery.py) |
| A09 | [call-control loop isolation](../services/backend/tests/test_control_event_loop.py), [owned HTTP lifetime/isolation](../services/backend/tests/test_http_runtime.py), [test_carrier_and_recovery](../services/backend/tests/test_carrier_and_recovery.py), [test_remediation_coach](../services/backend/tests/test_remediation_coach.py) |
| A10 | [test_telephony_workflows](../services/backend/tests/test_telephony_workflows.py), [test_remediation_rep_workflow](../services/backend/tests/test_remediation_rep_workflow.py) |
| A11 | [test_telephony_workflows](../services/backend/tests/test_telephony_workflows.py), [test_remediation_rep_workflow](../services/backend/tests/test_remediation_rep_workflow.py) |
| A12 | [test_remediation_boundaries](../services/backend/tests/test_remediation_boundaries.py), [test_admin_completion](../services/backend/tests/test_admin_completion.py) |
| A13 | [test_remediation_audit](../services/backend/tests/test_remediation_audit.py), [test_remediation_dnc_costs](../services/backend/tests/test_remediation_dnc_costs.py) |
| A14 | [test_remediation_audit](../services/backend/tests/test_remediation_audit.py), [test_optimization_numbers](../services/backend/tests/test_optimization_numbers.py) |
| A15 | [test_remediation_performance](../services/backend/tests/test_remediation_performance.py), [test_optimization_numbers](../services/backend/tests/test_optimization_numbers.py) |
| A16 | [test_remediation_performance](../services/backend/tests/test_remediation_performance.py) |
| A17 | [test_remediation_performance](../services/backend/tests/test_remediation_performance.py), [test_optimization_numbers](../services/backend/tests/test_optimization_numbers.py) |
| A18 | [test_optimization_numbers](../services/backend/tests/test_optimization_numbers.py), [test_scheduling_and_numbers](../services/backend/tests/test_scheduling_and_numbers.py) |
| A19 | [test_optimization_numbers](../services/backend/tests/test_optimization_numbers.py), [test_remediation_dnc_costs](../services/backend/tests/test_remediation_dnc_costs.py) |
| A20 | [test_remediation_dnc_costs](../services/backend/tests/test_remediation_dnc_costs.py), [test_remediation_learning](../services/backend/tests/test_remediation_learning.py) |
| A21 | [test_remediation_dnc_costs](../services/backend/tests/test_remediation_dnc_costs.py) |
| A22 | [test_remediation_coach](../services/backend/tests/test_remediation_coach.py), [test_remediation_audit](../services/backend/tests/test_remediation_audit.py) |
| A23 | [test_remediation_coach](../services/backend/tests/test_remediation_coach.py) |
| A24 | [test_remediation_coach](../services/backend/tests/test_remediation_coach.py), [test_remediation_boundaries](../services/backend/tests/test_remediation_boundaries.py), [test_remediation_audit](../services/backend/tests/test_remediation_audit.py) |
| A25 | [provider pool isolation](../services/backend/tests/test_http_runtime.py), [test_remediation_audit](../services/backend/tests/test_remediation_audit.py), [test_remediation_coach](../services/backend/tests/test_remediation_coach.py) |
| A26 | [test_openrouter_coach](../services/backend/tests/test_openrouter_coach.py), [test_remediation_coach](../services/backend/tests/test_remediation_coach.py) |
| A27 | [test_remediation_privacy](../services/backend/tests/test_remediation_privacy.py), [test_contextual_coaching](../services/backend/tests/test_contextual_coaching.py) |
| A28 | [test_remediation_boundaries](../services/backend/tests/test_remediation_boundaries.py), [test_contextual_coaching](../services/backend/tests/test_contextual_coaching.py) |
| A29 | [test_fetch_boundaries](../services/backend/tests/test_fetch_boundaries.py), [test_research_and_jobs](../services/backend/tests/test_research_and_jobs.py), [test_remediation_boundaries](../services/backend/tests/test_remediation_boundaries.py) |
| A30 | [test_batch_workflow](../services/backend/tests/test_batch_workflow.py), [test_remediation_rep_workflow](../services/backend/tests/test_remediation_rep_workflow.py) |
| A31 | [test_optimization_learning](../services/backend/tests/test_optimization_learning.py), [test_remediation_rep_workflow](../services/backend/tests/test_remediation_rep_workflow.py) |
| A32 | [test_remediation_audit](../services/backend/tests/test_remediation_audit.py), [test_optimization_learning](../services/backend/tests/test_optimization_learning.py) |
| A33 | [bounded model serving](../services/backend/tests/test_model_serving_bounds.py), [test_adaptive_model](../services/backend/tests/test_adaptive_model.py), [test_remediation_learning](../services/backend/tests/test_remediation_learning.py) |
| A34 | [test_remediation_learning](../services/backend/tests/test_remediation_learning.py), [test_optimization_learning](../services/backend/tests/test_optimization_learning.py) |
| A35 | [test_policy](../services/backend/tests/test_policy.py), [test_remediation_learning](../services/backend/tests/test_remediation_learning.py), [test_remediation_audit](../services/backend/tests/test_remediation_audit.py) |
| A36 | [test_remediation_audit](../services/backend/tests/test_remediation_audit.py), [test_remediation_privacy](../services/backend/tests/test_remediation_privacy.py) |
| A37 | [test_remediation_rep_workflow](../services/backend/tests/test_remediation_rep_workflow.py) |
| A38 | [test_remediation_rep_workflow](../services/backend/tests/test_remediation_rep_workflow.py), [test_remediation_learning](../services/backend/tests/test_remediation_learning.py) |
| A39 | [test_remediation_rep_workflow](../services/backend/tests/test_remediation_rep_workflow.py), [test_remediation_audit](../services/backend/tests/test_remediation_audit.py) |
| A40 | [indexed registry authorization](../services/backend/tests/test_registry_hotpath.py), [test_remediation_dnc_costs](../services/backend/tests/test_remediation_dnc_costs.py), [test_policy](../services/backend/tests/test_policy.py) |
| A41 | [test_remediation_privacy](../services/backend/tests/test_remediation_privacy.py) |
| A42 | [test_remediation_privacy](../services/backend/tests/test_remediation_privacy.py), [test_migrations](../services/backend/tests/test_migrations.py) |
| A43 | [test_remediation_privacy](../services/backend/tests/test_remediation_privacy.py), [test_remediation_rep_workflow](../services/backend/tests/test_remediation_rep_workflow.py) |
| A44 | [test_remediation_coach](../services/backend/tests/test_remediation_coach.py), [test_remediation_boundaries](../services/backend/tests/test_remediation_boundaries.py) |
| A45 | [test_operations](../services/backend/tests/test_operations.py), [test_remediation_rep_workflow](../services/backend/tests/test_remediation_rep_workflow.py), [workspace history](../services/backend/tests/test_workspace_pages.py), [workspace UI](../apps/desktop/tests/workspace-pages.spec.ts) |
| A46 | [test_connected_security](../services/backend/tests/test_connected_security.py), [test_remediation_rep_workflow](../services/backend/tests/test_remediation_rep_workflow.py) |
| A47 | [test_remediation_learning](../services/backend/tests/test_remediation_learning.py), [test_remediation_rep_workflow](../services/backend/tests/test_remediation_rep_workflow.py), [test_optimization_completion](../services/backend/tests/test_optimization_completion.py) |
| A48 | [test_remediation_learning](../services/backend/tests/test_remediation_learning.py), [test_remediation_rep_workflow](../services/backend/tests/test_remediation_rep_workflow.py) |
| A49 | [test_remediation_learning](../services/backend/tests/test_remediation_learning.py), [test_remediation_audit](../services/backend/tests/test_remediation_audit.py), [request accounting](../services/backend/tests/test_request_accounting.py), [billing resolution](../services/backend/tests/test_billing_resolution.py), [billing UI](../apps/desktop/tests/billing-resolution.spec.ts) |
| A50 | [current control/history bounds](../services/backend/tests/test_control_history.py), [registry hot paths](../services/backend/tests/test_registry_hotpath.py), [bounded model serving](../services/backend/tests/test_model_serving_bounds.py), [test_remediation_performance](../services/backend/tests/test_remediation_performance.py), [test_remediation_boundaries](../services/backend/tests/test_remediation_boundaries.py), [workspace history](../services/backend/tests/test_workspace_pages.py), [workspace UI](../apps/desktop/tests/workspace-pages.spec.ts) |
| A51 | [test_remediation_performance](../services/backend/tests/test_remediation_performance.py), [test_remediation_boundaries](../services/backend/tests/test_remediation_boundaries.py), [workspace history](../services/backend/tests/test_workspace_pages.py), [workspace UI](../apps/desktop/tests/workspace-pages.spec.ts) |
| A52 | [test_operations](../services/backend/tests/test_operations.py) |
| A53 | [test_migrations](../services/backend/tests/test_migrations.py) |
| A54 | [test_encrypted_storage](../services/backend/tests/test_encrypted_storage.py), [test_remediation_privacy](../services/backend/tests/test_remediation_privacy.py) |
| A55 | [test_admin_completion](../services/backend/tests/test_admin_completion.py) |
| A56 | [test_optimization_completion](../services/backend/tests/test_optimization_completion.py), [test_coach_replay](../services/backend/tests/test_coach_replay.py) |

Desktop journeys: [new proactive coaching/privacy/batch workflows](../apps/desktop/tests/remediation.spec.ts), [existing local/shared calling](../apps/desktop/tests/workflows.spec.ts), [optimization](../apps/desktop/tests/optimization.spec.ts), [live transport](../apps/desktop/tests/live.test.ts), and [cache boundaries](../apps/desktop/tests/cache-boundary.test.ts).

A01/A52–A55 also use [distribution contracts](../tests/test_distribution_contract.py), [native updater races](../tests/test_native_update_wait.py), Rust tests in src-tauri, the actual signed [packaged runtime probe](../scripts/frozen_acceptance.py), [Windows native acceptance](../scripts/windows_native_smoke.py) and installed Mac computer use. They are open for the new release until run against its exact bytes. Browser success is not native/hardware acceptance.

X01–X08 require the specified account, funded inference, retained authorization evidence, provider privacy terms or controlled devices/outcomes. No automated placeholder/skip can check those gates. A56 software tests validate reporting/replay contracts; independent human usefulness and actual outcome lift stay separate.

Initial latency fixtures retain the 503-lead regression and six 700ms concurrent jobs. Scaling profiles and repeated p95/resource measurements are implementation verification runs under section 18; query count has a hard test rather than a noisy single-request stopwatch assertion.

## October 18 execution assertion groups

The complete authored node lists are in [EXECUTION_TASKS.json](EXECUTION_TASKS.json). Existing tests are retained alongside new contracts; baseline capture is running before E01. None of these mappings accepts product or native delivery. Compiler-level missing types and explicitly pending installed-update receipts are recorded separately from behavioral failures. Each E section still owns all boundary assertions.

| Case | Step | Authored contract files | Mandatory assertion |
| --- | --- | --- | --- |
| C001 | E01 | `services/backend/tests/test_live_claim_review.py` | Suggestion, reason, alternative and implied premise each receive a current source-bound verdict; unsupported fields cannot stream. |
| C002 | E01 | `services/backend/tests/test_coach_admission.py` | 300-ms debounce admits latest prospect context without an eight-second wait; rep-only refresh remains paced; every limited opportunity counted. |
| C003 | E01 | `services/backend/tests/test_helper_progress.py`<br>`services/backend/tests/test_live_claim_review.py` | Generation/review have distinct ledger keys; source change or cancel withholds output and preserves submitted liability. |
| C004 | E01 | `services/backend/tests/test_coach_stream.py`<br>`services/backend/tests/test_live_claim_review.py` | Complete reviewed primary field may display; partial factual field may not; conflicting receipt withdraws exactly one card. |
| C005 | E02 | `services/backend/tests/test_conversation_finalization.py` | Twelve-minute call keeps minute-one derived need/promise after raw expiry and finalizes without an OpenAI key. |
| C006 | E02 | `services/backend/tests/test_conversation_finalization.py` | Omitted item is not resolved; correction invalidates dependants; expired evidence yields partial rather than invented replacement. |
| C007 | E02 | `services/backend/tests/test_call_rubrics.py` | Each frozen dimension has scored/N-A/insufficient state; chunks are not independent calls; coverage denominator is exact. |
| C008 | E02 | `services/backend/tests/test_tracker_publication.py` | Unknown speaker, foreign IDs, reference cycles and stale review cannot publish confirmed trackers or commitments. |
| C009 | E03 | `services/backend/tests/test_completion_journeys.py` | Import through next call, early callback, correction, restart, booking hold and cancellation yields one preserved task in local and real PostgreSQL modes. |
| C010 | E03 | `services/backend/tests/test_local_followthrough.py` | Lost response and repeated wrap-up resolve one canonical task; conflicting human-edited legacy duplicates remain explicit exceptions. |
| C011 | E03 | `services/backend/tests/test_local_followthrough.py` | Exact zoned date schedules; vague/missing-zone/DST fold requires confirmation; snooze never rewrites promised window. |
| C012 | E03 | `services/backend/tests/test_goal_reconciliation.py` | Booked/rescheduled holds; held evidence explicit; no-show/canceled require selected next action; no calendar/network write. |
| C013 | E03 | `services/backend/tests/test_coaching_library.py` | At most 20 reviewed opportunities and three goals; practice has no call outcome, training label or durable raw content. |
| C014 | E04 | `services/backend/tests/test_preparation_discovery.py` | Fact on services page succeeds; wrong-company/private redirect and injected instructions cannot; three pages/two optional searches/byte cap enforced. |
| C015 | E04 | `services/backend/tests/test_preparation_completion.py` | Shared HTTP/company/script limits hold with 100 queued leads; two failures preserve other results; controls stay responsive. |
| C016 | E04 | `services/backend/tests/test_preparation_completion.py` | Prepared count uses eligible inspected horizon; off-hours DST and one wake catch-up; no paid work when target satisfied. |
| C017 | E04 | `services/backend/tests/test_preparation_batch_completion.py`<br>`services/backend/tests/test_batch_recovery.py` | Same provider/custom IDs; only missing outputs requeued; each novel field reviewed once at its source version. |
| C018 | E05 | `services/backend/tests/test_screening_readiness.py` | Authorized cached prepaid-only endpoint can be ready; configured key without source/coverage cannot; preflight and dial agree. |
| C019 | E05 | `services/backend/tests/test_screening_refresh.py` | Two workers yield one submission; 500-row cursor resumes; opt-outs excluded; unknown charge prevents automatic repeat. |
| C020 | E05 | `services/backend/tests/test_registry_hotpath.py` | 500 NPA chunks require only relevant membership decode within LRU; atomic publication/expiry fences survive replacement. |
| C021 | E05 | `services/backend/tests/test_operating_validation.py`<br>`services/backend/tests/test_remediation_dnc_costs.py`<br>`services/backend/tests/test_remediation_privacy.py` | Settings merge cannot enable subscription/recording or longer raw retention; rates differ from actual charges; no paid GET. |
| C022 | E06 | `services/backend/tests/test_unified_planning.py` | Simple and named equivalent windows normalize identically; anonymous capacity cannot satisfy owned callback or double staff. |
| C023 | E06 | `services/backend/tests/test_unified_planning.py` | 80 general attempts remain 80 across regions; reservation-to-attempt replacement once; uncertain retries stay separate scenarios. |
| C024 | E06 | `services/backend/tests/test_unified_planning.py` | Independent exhaustive tiny fixtures match assignment/count; larger heuristic reports valid lower bound/gap and search budget. |
| C025 | E06 | `services/backend/tests/test_unified_planning.py` | 240/180/140 fixture, unequal durations, DST, pending setup, detached legs and stable bound-at-limit explain distinct shortfalls. |
| C026 | E07 | `services/backend/tests/test_number_reconciliation.py` | Incomplete pagination never deletes ownership; changed configuration invalidates callback evidence; partial order stays pending. |
| C027 | E07 | `services/backend/tests/test_optimization_numbers.py` | Sparse observations stay unknown; mix shifts do not imply spam; sustained comparable residual/drop triggers supported investigation. |
| C028 | E07 | `services/backend/tests/test_number_orders.py`<br>`services/backend/tests/test_remediation_dnc_costs.py`<br>`services/backend/tests/test_scheduling_and_numbers.py` | Purchase/enrollment denied before provider call; old order retains original reconciliation identity. |
| C029 | E07 | `services/backend/tests/test_scheduling_and_numbers.py` | Healthy retries retain identity; approved health change retains inbound old number and invalidates capacity proposal. |
| C030 | E08 | `services/backend/tests/test_optimization_learning.py` | Zero/few data use displayed priors; unknown role/failure excluded correctly; leave-cell-out pooling and circular hour formula match independent arithmetic. |
| C031 | E08 | `services/backend/tests/test_learning_chronology.py` | Future/corrected labels, overlapping outcomes, timestamp ties and shared entity cannot leak across training/calibration/evaluation. |
| C032 | E08 | `services/backend/tests/test_conversation_features.py` | Training-only bounded vocabulary; current/future call or unreviewed observation excluded; missing is not absent; ablation uses same rows. |
| C033 | E08 | `services/backend/tests/test_strategy_exposure.py` | Stable approved arms, actual allowed set/mixture probability, display-use distinction and matured reward; no invented sentence propensity. |
| C034 | E08 | `services/backend/tests/test_model_serving_bounds.py` | Owned checksummed artifacts retain original vectorizer; stages without support fall back; baseline imports no scientific stack. |
| C035 | E09 | `services/backend/tests/test_incremental_metrics.py` | Corrections/deletions/maturity/credits produce exact independent batch-oracle totals; unique leads and rep-day union not double counted. |
| C036 | E09 | `services/backend/tests/test_projection_replay.py` | Inactive namespace and cursor resume; concurrent dirty generations replay; same version no-op; old consistent snapshot remains available. |
| C037 | E09 | `services/backend/tests/test_job_admission.py` | More pending jobs than slots cannot expire while unstarted; heartbeat on actual claim; two workers do not execute one paid job twice. |
| C038 | E09 | `services/backend/tests/test_report_scaling.py` | GET/selection use scoped indexes and projections; no history-sized decryptions/global advisory lock/planning in writer. |
| C039 | E10 | `services/backend/tests/test_effect_matrix.py` | Each E10 operation times each five crash boundaries has original identity, fenced owner, cost state and meaningful next action; no blind repeat. |
| C040 | E10 | `services/backend/tests/test_inbound_completion.py`<br>`services/backend/tests/test_capability_consistency.py` | Owner, teammate, busy, ambiguous caller and external destination paths retain leg/task; offline local is unavailable without actual compatible proof. |
| C041 | E10 | `services/backend/tests/test_final_attempt_voicemail.py`<br>`services/backend/tests/test_effect_matrix.py` | Greatest declared allowance, actual submissions, late human, no beep, full mailbox, opt-out and detached restart preserve correct obligation. |
| C042 | E10 | `services/backend/tests/test_billing_resolution.py`<br>`services/backend/tests/test_request_accounting.py` | All actual legs/reviews/transfers and uncertain amounts reconcile once; credit period retained; optional budget cannot prevent stop. |
| C043 | E11 | `services/backend/tests/test_installed_copy_migration.py` | Copied actual schema authenticates; raw canaries removed before retained backup; IDs, human notes, denials and effect identities unchanged. |
| C044 | E11 | `services/backend/tests/test_remediation_privacy.py` | 299/300/301 seconds, retries/revisions/clock changes/wake/restart never extend raw life in any durable or transient boundary. |
| C045 | E11 | `services/backend/tests/test_effect_journal.py` | Replay typed encrypted effects/denials from independent journal; truncation, wrong key, fsync/disk-full and missing segments hold new effects. |
| C046 | E11 | `services/backend/tests/test_restore_effects.py` | Current effects/liabilities/human edits/auth win; conflicting identity aborts; old backups cannot resurrect revoked access. |
| C047 | E12 | `services/backend/tests/test_ingress_boundary.py` | Only signed webhook POST and active nonce GET pass; other methods/paths denied; arbitrary/private probe target refused. |
| C048 | E12 | `services/backend/tests/test_capability_consistency.py` | All UI/API capabilities reflect same evidence/source versions; missing keys/privacy/coverage remain actionable; GET never charges. |
| C049 | E12 | `services/backend/tests/test_connected_security.py`<br>`apps/desktop/tests/completion.spec.ts` | Switch held during active effects; no cross-authority cache/session or seller leakage; connected fixtures use real PostgreSQL. |
| C050 | E12 | `apps/desktop/tests/completion.spec.ts` | 1024x720 and1280x720 helper/script/hangup visible; F1/Escape/focus/opt-out work during AI failure and reconnect. |
| C051 | E13 | `services/backend/tests/test_completion_contracts.py` | Missing totals, branch floor alone, stale source and absent case receipts fail acceptance; docs-only digest equivalence explicit. |
| C052 | E13 | `services/backend/tests/test_completion_contracts.py` | Ten named mutations each fail their designated test; no mutated source enters production. |
| C053 | E13 | `services/backend/tests/test_completion_contracts.py` | Performance/AST tools reject installed paths, raw fields and unexpected network; parsers inventory whole production source. |
| C054 | E14 | `tests/test_distribution_contract.py` | 0.3.0 versions agree; keys/data identity unchanged; wrong signature/hash/platform rejected; fresh executable works without dev PATH. |
| C055 | E14 | `tests/test_native_update_wait.py` | Upgrader exited and installed backend SHA-256 equals accepted installer before native workflow; old GUI-only success insufficient. |
| C056 | E14 | `services/backend/tests/test_installed_copy_migration.py` | Mac/Windows installed update preserves actual allowed record IDs/note/denial/session and has no raw legacy data. |
| C057 | E15 | `services/backend/tests/test_completion_contracts.py` | Artifact/source/remote/evidence and cleanup allowlist agree; missing external receipt cannot become accepted-external. |
| C058 | E12 | `apps/desktop/src-tauri/src/lib.rs` | Validated argument arrays target the managed random loopback port; reconnect, missing SSH, mode switch and updater stop only the owned child; no token persisted. |
