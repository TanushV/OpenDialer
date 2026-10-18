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
| A50 | [registry hot paths](../services/backend/tests/test_registry_hotpath.py), [bounded model serving](../services/backend/tests/test_model_serving_bounds.py), [test_remediation_performance](../services/backend/tests/test_remediation_performance.py), [test_remediation_boundaries](../services/backend/tests/test_remediation_boundaries.py), [workspace history](../services/backend/tests/test_workspace_pages.py), [workspace UI](../apps/desktop/tests/workspace-pages.spec.ts) |
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
