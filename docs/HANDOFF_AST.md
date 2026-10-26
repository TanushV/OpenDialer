# OpenDialer code map (AST snapshot)

Generated from the 0.5.1 working tree (October 25, 2026) with Python's `ast` module (backend, tests, scripts) and a token count for TypeScript. It is a snapshot: regenerate sizes with `.venv/bin/python scripts/size.py`; line numbers drift as code changes. Read with [HANDOFF.md](HANDOFF.md).

## Size

| Area | Files | Lines | AST nodes / tokens | Baseline lines (a950675) | Change |
|---|---:|---:|---:|---:|---:|
| backend | 89 | 35,523 | 244,590 nodes | 39,459 | -10% lines, -10% nodes |
| backend_tests | 47 | 11,846 | 81,958 nodes | 52,034 | -77% lines |
| repo_tests | 4 | 668 | 3,992 nodes | 3,027 | -78% lines |
| frontend | 55 | 17,067 | 92,329 tokens | 16,487 | +4% lines, +3% tokens |
| frontend_tests | 51 | 9,860 | 71,507 tokens | 9,569 | +3% lines |
| rust | 2 | 971 | — | 2,463 | -61% lines |
| scripts | 11 | 1,392 | 7,764 nodes | 5,211 | -73% lines |
| workflows | 2 | 186 | — | 680 | -73% lines |

All Python together: 338,304 AST nodes (baseline about 687k). Targets in LEAN_PLAN.md: backend about -30% (missed, -9% nodes), frontend about -20% (missed, +4% tokens).

## HTTP routes

Every route the authority serves, from the route decorators. Access: `principal` = any signed-in user, `administrator` = owner/admin, `open` = no session dependency (it checks the local token, bootstrap token, Telnyx signature, its own socket ticket, or nothing for `/health`).

| Method | Path | Access | Handler |
|---|---|---|---|
| GET | `/auth/callback` | open | `app/main.py:1536` chatgpt_callback |
| GET | `/health` | open | `app/main.py:441` health |
| POST | `/v1/admin/backup` | administrator | `app/main.py:1127` backup |
| GET | `/v1/admin/billing/ledger` | administrator | `app/billing.py:427` ledger |
| POST | `/v1/admin/billing/ledger/{identity}/reconcile` | administrator | `app/billing.py:455` reconcile |
| POST | `/v1/admin/billing/reconcile` | administrator | `app/billing.py:459` carrier_records |
| PUT | `/v1/admin/budget` | administrator | `app/main.py:954` budget |
| GET | `/v1/admin/callback-routing` | administrator | `app/main.py:831` callback_routing |
| PUT | `/v1/admin/callback-routing` | administrator | `app/main.py:835` write_callback_routing |
| POST | `/v1/admin/coaching/replay` | administrator | `app/optimization/api.py:1007` replay |
| POST | `/v1/admin/consent` | administrator | `app/main.py:1336` consent |
| POST | `/v1/admin/control/run` | administrator | `app/main.py:1352` control_run |
| POST | `/v1/admin/dnc/screen` | administrator | `app/compliance/screening.py:608` screen |
| GET | `/v1/admin/dnc/screens` | administrator | `app/compliance/screening.py:556` screens |
| POST | `/v1/admin/dnc/screens/{identity}/retry` | administrator | `app/compliance/screening.py:622` retry |
| POST | `/v1/admin/ingress-check` | administrator | `app/main.py:823` ingress_check |
| POST | `/v1/admin/jobs/run` | administrator | `app/main.py:1123` run_jobs |
| POST | `/v1/admin/models/train` | administrator | `app/main.py:1389` train_model |
| POST | `/v1/admin/models/{model_id}/activate` | administrator | `app/main.py:1172` activate |
| POST | `/v1/admin/models/{model_id}/rollback` | administrator | `app/main.py:1395` rollback_model |
| POST | `/v1/admin/models/{model_id}/start-trial` | administrator | `app/optimization/api.py:604` start_trial |
| POST | `/v1/admin/models/{model_id}/stop-trial` | administrator | `app/optimization/api.py:676` stop_trial |
| POST | `/v1/admin/models/{model_id}/trial` | administrator | `app/main.py:1621` save_trial |
| POST | `/v1/admin/numbers` | administrator | `app/main.py:962` number |
| POST | `/v1/admin/numbers/proposal` | administrator | `app/main.py:1044` number_proposal |
| POST | `/v1/admin/numbers/search` | administrator | `app/main.py:1056` search_numbers |
| GET | `/v1/admin/numbers/summary` | administrator | `app/main.py:966` numbers_summary |
| POST | `/v1/admin/numbers/sync` | administrator | `app/main.py:1048` sync_numbers |
| PATCH | `/v1/admin/numbers/{number_id}` | administrator | `app/main.py:1022` edit_number |
| POST | `/v1/admin/numbers/{number_id}/configure` | administrator | `app/main.py:1052` configure_number |
| GET | `/v1/admin/numbers/{number_id}/review` | administrator | `app/optimization/api.py:793` number_evidence |
| POST | `/v1/admin/numbers/{number_id}/review` | administrator | `app/optimization/api.py:797` review_number |
| GET | `/v1/admin/operating-policy` | administrator | `app/compliance/operating.py:560` get_policy |
| PUT | `/v1/admin/operating-policy` | administrator | `app/compliance/operating.py:596` put_policy |
| PUT | `/v1/admin/optimization` | administrator | `app/optimization/api.py:493` configure |
| POST | `/v1/admin/optimization/comparisons` | administrator | `app/optimization/api.py:348` start_comparison |
| POST | `/v1/admin/optimization/comparisons/{comparison_id}/stop` | administrator | `app/optimization/api.py:454` stop_comparison |
| PUT | `/v1/admin/optimization/conversation-vocabulary` | administrator | `app/optimization/api.py:228` edit_conversation_vocabulary |
| POST | `/v1/admin/optimization/preview` | administrator | `app/optimization/api.py:481` preview |
| POST | `/v1/admin/optimization/refresh` | administrator | `app/optimization/api.py:596` refresh |
| POST | `/v1/admin/optimization/train` | administrator | `app/optimization/api.py:600` train |
| POST | `/v1/admin/optimization/trial` | administrator | `app/optimization/api.py:986` evaluate_trial |
| POST | `/v1/admin/playbooks` | administrator | `app/main.py:1293` publish_playbook |
| POST | `/v1/admin/policies/{policy_id}/publish` | administrator | `app/main.py:802` publish |
| GET | `/v1/admin/preflight` | administrator | `app/main.py:819` preflight |
| GET | `/v1/admin/recovery` | administrator | `app/main.py:1105` recovery_items |
| POST | `/v1/admin/recovery` | administrator | `app/main.py:1101` recovery |
| POST | `/v1/admin/recovery/agents/{identity}` | administrator | `app/main.py:1113` recover_agent |
| POST | `/v1/admin/recovery/commands/{identity}` | administrator | `app/main.py:1109` recovery_action |
| POST | `/v1/admin/recovery/jobs/{identity}/retry` | administrator | `app/main.py:1119` retry_job |
| POST | `/v1/admin/registry/import` | administrator | `app/main.py:1231` registry_import |
| POST | `/v1/admin/restore` | administrator | `app/main.py:1131` restore |
| POST | `/v1/admin/restore/reconcile` | administrator | `app/main.py:1617` reconcile_restore |
| POST | `/v1/admin/rnd/results` | administrator | `app/main.py:1450` import_rnd |
| GET | `/v1/admin/rubrics` | administrator | `app/postcall/rubrics.py:302` get_config |
| PUT | `/v1/admin/rubrics` | administrator | `app/postcall/rubrics.py:307` save_config |
| POST | `/v1/admin/script/import` | administrator | `app/optimization/script_lab.py:651` import_script |
| PUT | `/v1/admin/script/memory` | administrator | `app/optimization/script_lab.py:675` edit_memory |
| POST | `/v1/admin/script/proposals` | administrator | `app/optimization/script_lab.py:704` propose |
| POST | `/v1/admin/script/proposals/{proposal_id}/approve` | administrator | `app/optimization/script_lab.py:735` approve |
| POST | `/v1/admin/script/proposals/{proposal_id}/reject` | administrator | `app/optimization/script_lab.py:753` reject |
| POST | `/v1/admin/script/proposals/{proposal_id}/start` | administrator | `app/optimization/script_lab.py:721` start |
| POST | `/v1/admin/script/rollback` | administrator | `app/optimization/script_lab.py:768` rollback |
| PUT | `/v1/admin/script/settings` | administrator | `app/optimization/script_lab.py:686` edit_settings |
| POST | `/v1/admin/seller/approve` | administrator | `app/main.py:1313` approve_seller |
| GET | `/v1/admin/settings` | administrator | `app/main.py:850` read_settings |
| PUT | `/v1/admin/settings` | administrator | `app/main.py:899` write_settings |
| POST | `/v1/admin/snippets` | administrator | `app/main.py:1442` publish_snippet |
| PATCH | `/v1/admin/snippets/{example_id}` | administrator | `app/main.py:1446` withdraw_snippet |
| POST | `/v1/admin/suppressions/release-hold` | administrator | `app/main.py:598` release_hold |
| POST | `/v1/admin/trackers` | administrator | `app/main.py:1356` publish_tracker |
| POST | `/v1/admin/users` | administrator | `app/main.py:1135` create_user |
| POST | `/v1/admin/workspaces` | administrator | `app/main.py:1166` create_workspace |
| POST | `/v1/auth/logout` | principal | `app/main.py:496` logout |
| GET | `/v1/auth/quiescence` | principal | `app/main.py:510` quiescence |
| POST | `/v1/auth/session` | open | `app/main.py:477` login |
| POST | `/v1/callbacks/{task_id}/hangup` | principal | `app/main.py:839` end_callback |
| POST | `/v1/calls/start` | principal | `app/main.py:553` start_call |
| GET | `/v1/calls/{call_id}` | principal | `app/main.py:557` get_call |
| GET | `/v1/calls/{call_id}/analysis` | principal | `app/main.py:667` analysis |
| PATCH | `/v1/calls/{call_id}/coach` | principal | `app/main.py:627` coach_preferences |
| POST | `/v1/calls/{call_id}/coach` | principal | `app/main.py:639` coach |
| POST | `/v1/calls/{call_id}/coach/cancel` | principal | `app/main.py:649` cancel_coach |
| GET | `/v1/calls/{call_id}/coach/feedback` | principal | `app/optimization/api.py:880` read_feedback |
| POST | `/v1/calls/{call_id}/coach/feedback` | principal | `app/optimization/api.py:897` feedback |
| POST | `/v1/calls/{call_id}/comments` | principal | `app/main.py:677` comment |
| POST | `/v1/calls/{call_id}/disposition` | principal | `app/main.py:586` disposition |
| POST | `/v1/calls/{call_id}/dtmf` | principal | `app/main.py:1305` dtmf |
| GET | `/v1/calls/{call_id}/dtmf/{request_id}` | principal | `app/main.py:1309` keypad_status |
| POST | `/v1/calls/{call_id}/hangup` | principal | `app/main.py:582` hangup |
| PATCH | `/v1/calls/{call_id}/notes` | principal | `app/main.py:671` edit_notes |
| POST | `/v1/calls/{call_id}/observations/{observation_id}/review` | principal | `app/optimization/api.py:265` review_observation |
| PUT | `/v1/calls/{call_id}/rubric` | principal | `app/postcall/rubrics.py:329` change_purpose |
| POST | `/v1/calls/{call_id}/transcript` | principal | `app/main.py:620` transcript |
| GET | `/v1/coaching` | principal | `app/main.py:659` coaching |
| PUT | `/v1/coaching/goals` | principal | `app/main.py:663` coaching_goals |
| POST | `/v1/coaching/practice` | principal | `app/main.py:653` practice |
| GET | `/v1/definitions` | principal | `app/main.py:845` read_definitions |
| GET | `/v1/events` | principal | `app/main.py:692` events |
| POST | `/v1/inference/chatgpt/complete` | principal | `app/main.py:1503` chatgpt_complete |
| POST | `/v1/inference/chatgpt/import` | principal | `app/main.py:1578` import_chatgpt_profile |
| GET | `/v1/inference/chatgpt/models` | principal | `app/main.py:1598` chatgpt_models |
| POST | `/v1/inference/chatgpt/start` | principal | `app/main.py:1485` chatgpt_start |
| POST | `/v1/leads/import` | administrator | `app/main.py:604` import_leads |
| GET | `/v1/leads/{lead_id}/conversions` | principal | `app/optimization/api.py:698` list_conversions |
| POST | `/v1/leads/{lead_id}/conversions` | principal | `app/optimization/api.py:714` conversion |
| POST | `/v1/leads/{lead_id}/conversions/{event_id}/retract` | principal | `app/optimization/api.py:758` retract_conversion |
| PATCH | `/v1/leads/{lead_id}/endpoint` | administrator | `app/main.py:1237` correct_endpoint |
| POST | `/v1/leads/{lead_id}/meeting` | principal | `app/main.py:590` report_meeting |
| PATCH | `/v1/leads/{lead_id}/outreach` | principal | `app/main.py:1301` review_outreach |
| GET | `/v1/leads/{lead_id}/packet` | principal | `app/main.py:608` packet |
| WEBSOCKET | `/v1/live` | open | `app/main.py:738` live |
| POST | `/v1/local/session` | open | `app/main.py:467` local_session |
| GET | `/v1/metrics` | principal | `app/main.py:525` metrics |
| GET | `/v1/optimization` | principal | `app/optimization/api.py:344` report |
| GET | `/v1/optimization/conversations` | principal | `app/optimization/api.py:204` conversation_report |
| GET | `/v1/optimization/script` | principal | `app/optimization/script_lab.py:647` script_lab |
| GET | `/v1/optimization/script/memory.md` | principal | `app/optimization/script_lab.py:667` script_memory |
| GET | `/v1/optimization/strategies` | principal | `app/optimization/api.py:449` strategies |
| GET | `/v1/preparation` | principal | `app/main.py:612` preparation |
| POST | `/v1/queue/next` | principal | `app/main.py:549` next_lead |
| POST | `/v1/rep/heartbeat` | principal | `app/main.py:537` heartbeat |
| POST | `/v1/rep/presence` | principal | `app/main.py:533` presence |
| GET | `/v1/rep/work` | principal | `app/main.py:541` work_intervals |
| PATCH | `/v1/rep/work/{interval_id}` | principal | `app/main.py:545` edit_work |
| POST | `/v1/research/batches` | administrator | `app/main.py:616` research |
| POST | `/v1/scorecards/{scorecard_id}/review` | principal | `app/postcall/review.py:87` save_review |
| POST | `/v1/setup` | open | `app/main.py:453` setup |
| POST | `/v1/simulation/event` | principal | `app/main.py:775` simulation_event |
| POST | `/v1/simulation/seed` | administrator | `app/main.py:529` seed |
| POST | `/v1/suppressions` | principal | `app/main.py:594` suppress |
| PATCH | `/v1/tasks/{task_id}` | principal | `app/main.py:1297` task_status |
| POST | `/v1/telephony/agent` | principal | `app/main.py:1340` agent_leg |
| GET | `/v1/telephony/agent/{provider_id:path}` | principal | `app/main.py:1344` agent_status |
| POST | `/v1/telephony/token` | principal | `app/main.py:781` telephony_token |
| GET | `/v1/workspace` | principal | `app/main.py:515` workspace |
| GET | `/v1/workspace/{section}` | principal | `app/main.py:519` workspace_page |
| GET | `/webhooks/ingress-check/{nonce}` | open | `app/main.py:827` ingress_challenge |
| POST | `/webhooks/telnyx` | open | `app/main.py:1197` webhook |

139 route declarations.

## Backend modules (`services/backend/app`)

### `__init__.py` (0 lines, 1 nodes)

- (constants only)

### `accounting.py` (220 lines, 1,313 nodes)

One budget liability per provider attempt, settled by an attributable receipt.

- class `RequestAccounting` :11 — _request_hold:12, reserve_optional_budget:22, reserve_budget_in:28, usage:79, settle_estimate_in:178, release_refused_budget:199, release_unsubmitted_budget:214

### `administration.py` (995 lines, 7,066 nodes)

- `capability_statuses()` :33
- class `Dialer`(WorkspaceReads, ReportProjections, RecoveryActions, CommandRecovery, InboundCallbacks, Measurement, RepWorkflows, NumberOperations, WebhookIngress, ScriptLab, OptimizationWorkflows, TelephonyWorkflows, Application) :149 — preflight:164, database_integrity:282, _preflight_queue:289, _ingress_start:345, ingress_challenge:417, _ingress_finish:436, _ingress_failed:492, check_ingress:498, publish:519, add_number:556, edit_number:589, proposal:688, backup:759, restore:803, _restore_operator:904, reconcile_restore:909

### `billing.py` (460 lines, 3,094 nodes)

Local receipt review; never invokes a provider or treats a partial CDR as a final invoice.

- class `Strict`(BaseModel) :21
- class `ReceiptEdit`(Strict) :25
- class `CarrierRecord`(Strict) :39 — one_identity:50
- class `CarrierImport`(Strict) :56
- `scoped()` :61
- `by_key()` :66
- `net_cost()` :70
- `view()` :76
- `proof_key()` :113
- `fingerprint()` :117
- `prior_proof()` :121
- `store_proof()` :128
- `account_rate()` :154
- `call_estimate()` :162
- `settle_call_estimate()` :180
- `close_call()` :207
- `reconcile_entry()` :235
- `import_records()` :303
- `bind_api()` :425

### `cli.py` (200 lines, 1,115 nodes)

- class `AuthorityBusy`(RuntimeError) :20
- `authority_lock()` :25
- `read_launch_secrets()` :54
- `_launcher_lost()` :87
- `watch_launcher()` :95
- `_no_lock()` :112
- `main()` :116
- `run()` :153

### `coaching/__init__.py` (0 lines, 1 nodes)

- (constants only)

### `coaching/context.py` (932 lines, 6,510 nodes)

Bounded conversational context; semantic interpretation belongs to the configured model.

- `memory_id()` :24
- `words()` :33
- `evidence()` :61
- `build_context()` :69
- `retrieve_cards()` :111
- class `MeaningReviewRequired`(CardRejected) :130
- `guidance_sources()` :134
- `guidance_fields()` :218
- `nonassertive_fields()` :226
- `guidance_review_request()` :242
- `ground_guidance()` :263
- `material_update()` :331
- `reviewed_guidance()` :347
- `upgrade_state()` :387
- `lineage_key()` :465
- `lineage_metadata()` :469
- `segment_revisions()` :491
- `state_view()` :506
- `persistent_state()` :531
- `interpretation_delta()` :567
- `merge_semantic_state()` :623
- `invalidate_state()` :867

### `coaching/contracts.py` (179 lines, 1,133 nodes)

Provider-neutral conversation transport. Private text never belongs in receipts.

- class `StrictModel`(BaseModel) :10
- class `AuditSource`(StrictModel) :14
- class `FieldAudit`(StrictModel) :22
- class `Guidance`(StrictModel) :29
- class `SourceRef`(AuditSource) :55
- class `ProposedCommitment`(StrictModel) :62
- class `ProposedOutcome`(StrictModel) :70
- class `ObservationDelta`(StrictModel) :75
- class `RubricDelta`(StrictModel) :89
- class `TrackerDelta`(StrictModel) :97
- class `StateDelta`(StrictModel) :103
- class `Observation`(StrictModel) :111
- class `ConversationStateV2`(StrictModel) :135
- class `TurnOutput`(StrictModel) :152
- `card_view()` :162

### `coaching/engine.py` (228 lines, 1,411 nodes)

- class `CardRejected`(ValueError) :15
- `monotonic()` :19
- `transport_fault()` :24
- `failure()` :31
- `finish_shielded()` :39
- `validate_card()` :53
- class `CoachEngine` :76 — __init__:77, _circuit:87, cancel:99, current:109, suggest:114

### `coaching/knowledge.py` (67 lines, 558 nodes)

Versioned seller knowledge; facts constrain the helper, not exact wording.

- class `Example`(BaseModel) :13
- class `Edit`(BaseModel) :21 — unique_examples:36
- `publish()` :42

### `coaching/library.py` (345 lines, 2,428 nodes)

Small, reviewed teaching library and personal goals in the existing store.

- class `Strict`(BaseModel) :16
- class `Goal`(Strict) :20
- class `GoalsEdit`(Strict) :26
- class `ExampleReview`(Strict) :31
- class `ExampleWithdraw`(Strict) :38
- `examples()` :43
- `context_examples()` :91
- `overview()` :108
- `save_goals()` :232
- `publish()` :255
- `withdraw()` :324

### `coaching/replay.py` (218 lines, 1,464 nodes)

Reproducible inference-only replay; real-call usefulness needs a reviewed reference set.

- `encoded()` :44
- `validate_cases()` :48
- `evaluate()` :107

### `coaching/requests.py` (133 lines, 1,211 nodes)

Bounded private-help receipts. Raw questions/answers never enter this store.

- `request_key()` :11
- `_load()` :15
- `_new()` :40
- `begin()` :55
- `finish()` :81
- `cancel()` :101
- `current()` :120

### `coaching/sources.py` (89 lines, 701 nodes)

Small content-free dependency receipts for current guidance.

- `digest()` :13
- `knowledge()` :17
- `knowledge_current()` :25
- `lead_context()` :37
- `current()` :41

### `coaching/workflows.py` (1,329 lines, 8,996 nodes)

Proactive/private help using one bounded context and one accountable inference.

- class `LiveHelper` :38 — _conversation_lineage:39, _live_lineage:48, _store_conversation:88, _queue_conversation_review:156, _defer_help:193, _coach_authority:211, practice:223, _practice_authorized:384, _practice_knowledge_current:394, _finish_practice:402, coach_content:425, visible_help:430, _reserve_coach:464, _settle_coach:478, _help_allowed:495, _help_context:504, cancel_help:768, coach_turn:771, _coach_turn:807, _admit_help:936, _automatic_admissions_in:964, _defer_busy_help:982, _validated_help:989, _review_help:1072, _withdraw_help:1139, _publish_help:1149

### `compliance/__init__.py` (0 lines, 1 nodes)

- (constants only)

### `compliance/consent.py` (166 lines, 1,208 nodes)

Scoped consent evidence; a revocation immediately stops the permissions it names.

- class `ConsentChange`(BaseModel) :16 — reference:29, scopes:36
- `request_hash()` :42
- `permission_state()` :46
- `revoke_effects()` :65
- `record()` :116

### `compliance/operating.py` (659 lines, 3,994 nodes)

No-recurring-spend policy and seller-authorized, cached prepaid screening.

- class `DncRefreshPolicy`(BaseModel) :19 — explicit_funding:28
- class `AccountRate`(BaseModel) :34 — source_not_blank:44
- class `OperatingPolicy`(BaseModel) :50 — known_rate_units:67
- class `OperatingEdit`(OperatingPolicy) :78
- class `ScreeningRequest`(BaseModel) :83
- class `DNCAuthorization`(BaseModel) :88 — reference_not_blank:98, valid_area_codes:105, zoned_timestamp:112
- `cost_register()` :238
- `authority_stamp()` :250
- `source_expiry()` :256
- `screening_capability()` :274
- `screening_snapshot()` :402
- `screening_key()` :439
- `screening_page()` :443
- `prepaid_snapshot()` :491
- `authorized_screen()` :504
- `cached_screen()` :509
- `screening_max_age()` :538
- `screening_expiry()` :543
- `usable_screen()` :553
- `bind_operating_api()` :558

### `compliance/phone.py` (27 lines, 132 nodes)

- class `PhoneResult` :7
- `normalize_phone()` :16

### `compliance/policy.py` (99 lines, 728 nodes)

- class `CallContext` :7
- class `Decision` :45
- `authorize()` :50

### `compliance/registry.py` (326 lines, 2,542 nodes)

Imported do-not-call registries: one immutable bitmap chunk per area code, published atomically.

- class `RegistryError`(ValueError) :17
- `published_snapshots()` :21
- `valid_endpoint()` :42
- `_chunk()` :46
- `registry_contains()` :73
- `publication_receipt()` :80
- `_validated()` :91
- `publish_registry()` :132
- class `RegistryBitmap` :287 — __init__:290, build:294, contains:304, dump:311, load:316

### `compliance/screening.py` (624 lines, 4,425 nodes)

One leased prepaid lookup per phone; uncertain effects require an explicit new charge choice.

- class `ScreeningRetry`(BaseModel) :37
- `active_admin()` :44
- `lease_active()` :49
- `view()` :57
- `claim()` :89
- `query_charge()` :206
- `finish()` :225
- `reserve_refresh_in()` :296
- `schedule_refresh()` :325
- `refresh_page()` :375
- `refresh_progress()` :411
- `refresh_screening()` :451
- `execute()` :514
- `bind_screening_api()` :554

### `compliance/suppressions.py` (158 lines, 1,179 nodes)

Exact opt-out receipts and their effects on reservations and live calls.

- `identity()` :13
- `fingerprint()` :17
- `apply_in()` :21
- `release_hold()` :102
- `record()` :144

### `config.py` (95 lines, 446 nodes)

- class `ProviderDefaults`(BaseModel) :8
- `inference_model()` :23
- class `LIMITS` :32
- class `CapabilityStatus`(BaseModel) :57
- class `Settings`(BaseSettings) :70

### `db.py` (609 lines, 3,393 nodes)

- `uid()` :47
- `utcnow()` :51
- class `Base`(DeclarativeBase) :55
- class `Document` :59 — __mapper_args__:69
- class `ReportRevision`(Base) :192
- class `ActiveClaim`(Base) :199
- class `Event`(Base) :218
- class `Job`(Base) :230
- `plan_statistics()` :258
- class `OldStore`(RuntimeError) :295
- class `Database` :299 — __init__:300, plan:331, transaction:338, snapshot:386, authorize_stream:401, reset:415, integrity:419
- `rows()` :427
- `prefetch()` :444
- `index_pending_rows()` :470
- `report_revision_last()` :484
- `announce_job_changes()` :492
- `insert_jobs()` :500
- `get()` :515
- `put()` :520
- `after_key()` :540
- `insert_absent()` :549
- `update()` :565
- `doc()` :571
- `emit()` :575
- `audit()` :597
- `digest()` :608

### `grounding.py` (159 lines, 871 nodes)

A complete, source-bound publication review; model review is not factual proof.

- class `Verdict`(BaseModel) :32
- class `ReviewOutput`(BaseModel) :39
- `review_request()` :45
- class `ReviewReceipt` :53 — metadata:59
- `accept_review()` :72
- `require_review()` :112
- `review_generated()` :117

### `intelligence.py` (2,288 lines, 15,448 nodes)

- class `Application`(Preparation, LiveHelper, SpeechPrivacy, RequestAccounting, Service) :81 — inference:82, approved_playbook:123, fallback_script:136, packet:173, queue_research:301, prepare:316, prepare_one:347, research_current:602, research_company:619, _research_company:638, _extract_company_facts:975, configured_coach:1077, conversation_view_in:1090, note_view_in:1111, _materialize_views_in:1124, analysis:1200, edit_notes:1295, run_jobs:1326, _training_source:1505, _training_snapshot:1551, _training_publish:1555, train_model_job:1580, periodic_optimization:1605, maintenance_tick:1616, tracker_matches:1672, _conversation_signature:1699, enqueue_finalize:1715, _carry_finalization:1725, _begin_finalization:1739, _conversation_current_in:1768, _conversation_request_current:1818, _conversation_context:1823, _conversation_infer:1829, _merge_finalization:1871, _finish_finalization:1920, _publish_unavailable_rundown:1961, analyze_postcall:2008, _state_review_snapshot:2054, _publish_state_review:2165, review_conversation:2240, host_id:2280

### `jobs/__init__.py` (0 lines, 1 nodes)

- (constants only)

### `jobs/queue.py` (312 lines, 2,046 nodes)

- `aware()` :9
- class `LostJobLease`(ValueError) :22
- `jobdoc()` :26
- class `JobQueue` :41 — __init__:42, require_owned_in:45, enqueue:58, enqueue_in:70, claim_many:138, coalesce_live_in:159, complete:202, fail:205, coalesce_refresh:208, _finish:219, heartbeat:269, next_due:282, recover_expired:289, get:310

### `main.py` (1,665 lines, 10,911 nodes)

- class `Strict`(BaseModel) :38
- class `RecoveryEdit`(Strict) :42 — attested:49
- class `JobRetry`(Strict) :55
- class `IngressCheck`(Strict) :59
- class `Setup`(Strict) :64
- class `CoachPreferences`(Strict) :72
- class `CoachQuestion`(Strict) :77
- class `CoachCancel`(Strict) :83
- class `PracticeRequest`(Strict) :87
- class `Login`(Strict) :92
- class `Presence`(Strict) :97
- class `Heartbeat`(Strict) :105
- class `QueueNext`(Strict) :109
- class `CallStart`(Strict) :113
- class `KeypadRequest`(Strict) :118
- class `Suppress`(Strict) :123
- class `HoldRelease`(Strict) :129
- class `ImportLeads`(Strict) :134
- class `Transcript`(Strict) :139
- class `PlanCompletion`(Strict) :155
- class `NoteEdit`(Strict) :159 — one_version:166
- class `Disposition`(Strict) :172
- class `ResearchBatch`(Strict) :183
- class `SimulationEvent`(Strict) :187
- class `Budget`(Strict) :194
- class `NumberInput`(Strict) :200
- class `PolicyUpdate`(Strict) :208
- class `ProviderSettings`(Strict) :231
- `write_conflict()` :280
- `_bound()` :291
- `field_bounds()` :306
- `setting_bounds()` :321
- `create_app()` :357

### `measurement.py` (372 lines, 2,827 nodes)

Shared outcome definitions and transparent, heartbeat-supported working intervals.

- `work_summary()` :21
- `connect_health_counts()` :71
- class `WorkEdit`(BaseModel) :98 — time_with_zone:107
- class `Measurement` :116 — record_attempt_work_in:117, open_work_in:161, pause_work_in:174, end_wrapup_work_in:184, record_work_in:192, heartbeat:238, work_intervals:303, edit_work:317
- `call_timing()` :352

### `ml/__init__.py` (0 lines, 1 nodes)

- (constants only)

### `ml/serving.py` (185 lines, 1,439 nodes)

Pure-Python scoring of exported funnel models; no NumPy, scikit-learn, joblib or pickle.

- class `ArtifactError`(ValueError) :30
- `encode_threshold()` :34
- `expit()` :44
- class `Stage` :52 — __init__:55, probabilities:59
- `_reject_constant()` :83
- `_number()` :87
- `_index()` :93
- `_flag()` :99
- `_strings()` :105
- `_tree()` :111
- `stage_from_document()` :141
- `parse()` :173

### `ml/training.py` (722 lines, 5,605 nodes)

Bounded CPU funnel challengers with independent chronological calibration.

- `fitted_vocabulary()` :54
- `feature_order()` :70
- `vector_v2()` :88
- `label_time()` :117
- `linked_entities()` :128
- `partition_at()` :152
- `calibration_diagnostics()` :175
- `train_challenger()` :226
- `paired_bootstrap()` :233
- `stage_partitions()` :265
- `stage_supported()` :283
- `training_support()` :295
- `fit_stage()` :311
- `export_stage()` :376
- `rolling_cutoffs()` :443
- `baseline_probabilities()` :459
- `_train_challenger()` :471
- `promotion_allowed()` :657
- `_verified_stages()` :668
- `predict_funnel_batch()` :691
- `predict_funnel()` :714

### `ml/worker.py` (236 lines, 1,349 nodes)

One temporary CPU child; no authority, secrets, network clients or extra service.

- `trainer_path()` :28
- `trainer_unavailable()` :35
- `child_command()` :45
- `child_environment()` :53
- `encode()` :76
- `training_slot()` :87
- `exchange()` :105
- `reap()` :132
- `checked_result()` :145
- `train_isolated()` :157
- `main()` :205

### `number_operations.py` (652 lines, 4,697 nodes)

Reconcile owned inventory and one durable configuration intent per number/route.

- class `ExternalReputation`(BaseModel) :17 — ordered:26
- `carrier_rejection()` :32
- class `NumberOperations` :52 — number_owners:53, number_readiness_in:68, number_review_view:99, number_workload_ceiling:143, confirm_callback_in:152, reassign_number:181, _configuration_actor:245, sync_numbers:250, _merge_inventory:255, _claim_number_configuration:361, _allow_configuration_retry:431, configure_owned_number:452, _configuration_receipt:503, _claim_number_check:525, reconcile_number_configuration:560, _finish_number_configuration:573

### `operational_schema.py` (405 lines, 2,114 nodes)

Indexed columns copied from each document so work is selected without scanning history.

- `page_indexes()` :31
- `context_key()` :81
- `context_columns()` :89
- `context_indexes()` :98
- `columns()` :122
- `indexes()` :161
- `receipt_key()` :267
- `as_time()` :273
- `call_work_bounds()` :280
- `sync_columns()` :299

### `optimization/__init__.py` (1 lines, 3 nodes)

Operational allocation and conversion learning shared by both authority modes.

- (constants only)

### `optimization/api.py` (1,127 lines, 8,294 nodes)

- class `Strict`(BaseModel) :17
- class `StaffedWindow`(Strict) :21 — ordered:31
- class `OptimizationSettings`(Strict) :41 — distinct_windows:68
- class `Conversion`(Strict) :92
- class `NumberReview`(Strict) :101
- class `Retraction`(Strict) :109
- class `Feedback`(Strict) :113
- class `Empty`(Strict) :129
- class `ModelTrial`(Strict) :133
- class `Replay`(Strict) :139 — bounded:144
- class `Comparison`(Strict) :151 — finite:162
- class `ConversationAlias`(Strict) :177
- class `VocabularyEdit`(Strict) :183 — validate_aliases:189
- class `ObservationReview`(Strict) :196
- `register_optimization()` :202

### `optimization/capacity.py` (1,401 lines, 12,186 nodes)

One bounded forecast over immutable inputs; never permission to place a call.

- `_field_names()` :34
- `_canonical()` :38
- `input_fingerprint()` :45
- class `Availability` :62
- class `AttemptWork` :71 — value:91
- class `NumberCapacity` :96 — limit:114
- class `PlanningInput` :120
- class `PlanningResult` :138
- class `SearchBudget` :158 — spend:165
- `priority_class()` :175
- `work_items()` :185
- `normalize_availability()` :274
- `staffed_slots()` :313
- `number_input()` :355
- `run_starts()` :385
- `lowest()` :395
- `served()` :399
- `schedule_value()` :407
- class `Assignment` :411 — __init__:412, local_times:449, window_mask:455, steps:479, exhausted:483, cell_value:486, value_at:492, value_groups:495, counts:506, solve:513, schedule_rows:844
- `addition_lower_bound()` :869
- `count_ceiling()` :876
- `contingent_retries()` :881
- `additional_capacity()` :970
- `retry_capacity()` :1041
- `time_forecast()` :1140

### `optimization/conversations.py` (357 lines, 2,931 nodes)

Bounded, descriptive learning from reviewed conversation observations.

- `normalized()` :16
- `aliases_for()` :20
- `prior_features()` :33
- `active_observations()` :101
- `_mix()` :112
- `_comparison()` :121
- `first_eligible()` :163
- `association_patterns()` :202
- `supported()` :244
- `conversation_associations()` :254
- `invalidate_report()` :326
- `guidance_context()` :338

### `optimization/experiments.py` (496 lines, 3,335 nodes)

Stable optional experiments; assignments precede treatment and never change permissions.

- `strategy_rewards()` :22
- `strategy_choice()` :54
- `observed_strategy_uses()` :86
- `assign_arm()` :110
- `call_goal_events()` :126
- `reward_values()` :151
- `exposure_values()` :155
- `comparison_from_counts()` :177
- class `ComparisonWorkflows` :275 — strategy_exposures_in:276, strategy_catalog:309, strategy_context:320, comparison_assignment:372

### `optimization/goals.py` (83 lines, 667 nodes)

One goal reconciliation calculation for previews and committed changes.

- `review_token()` :9
- `goal_state()` :25
- `goal_plan()` :44

### `optimization/learning.py` (674 lines, 5,304 nodes)

- `hashed_draw()` :27
- `outcome_at()` :32
- `conversion_at()` :48
- `freeze_features()` :76
- `training_rows()` :118
- `funnel_predictions()` :233
- `prior_means()` :241
- `conversion_score()` :249
- `retry_statistics()` :260
- `reward_counts()` :332
- `distribution_from_counts()` :356
- `drift_stratum()` :413
- `drift_values()` :427
- `drift_from_counts()` :451
- `trial_values()` :535
- `trial_from_counts()` :561
- `stable_control()` :618
- `cohort_fraction()` :622
- `cohort_arm()` :631
- `choose_cohort_candidate()` :636

### `optimization/numbering.py` (464 lines, 2,999 nodes)

- class `HealthPolicy`(BaseModel) :13
- `timestamp()` :24
- `health_values()` :31
- `health_stratum()` :80
- `health_from_counts()` :87
- `_observed_health()` :154
- `assess_number_health()` :175
- `number_health()` :282
- `route_fingerprint()` :305
- `callback_current()` :310
- `number_ready()` :329
- `select_number()` :352
- `capacity_forecast()` :412
- `adaptive_daily_limit()` :455

### `optimization/projections.py` (1,372 lines, 9,358 nodes)

Report totals computed directly from source records.

- `lead_values()` :38
- `call_values()` :90
- `training_values()` :137
- `_microusd()` :164
- `usage_values()` :176
- `observed_call_cost()` :193
- `_call_cost_group()` :204
- `help_values()` :255
- `coaching_from_counts()` :276
- `definition_version()` :305
- `_trial_groups()` :323
- `_comparison_groups()` :402
- `_health_groups()` :507
- `_detail_groups()` :554
- `_calculate_unit()` :668
- class `Totals` :728 — __init__:731, group:735, add:744, due:750, buckets:754, total:773, rep_total:776
- `_key()` :780
- `_scoped()` :784
- `_docs()` :791
- `_chunks()` :795
- `_record()` :801
- `compute()` :836
- `read_coaching_metrics()` :1106
- `read_dataset_metrics()` :1110
- `read_estimate_statistics()` :1119
- `read_drift_metrics()` :1158
- `read_number_health_metrics()` :1188
- `read_comparison_metrics()` :1213
- `read_trial_metrics()` :1247
- `read_metrics()` :1295

### `optimization/script_lab.py` (781 lines, 5,427 nodes)

The campaign's script lab: base script versions, the Markdown memory, proposals and script tests.

- class `Strict`(BaseModel) :45
- class `ProposalBody`(Strict) :49
- class `TestBody`(Strict) :62
- class `SettingsBody`(Strict) :68
- class `ImportBody`(Strict) :73
- class `NotesBody`(Strict) :79
- class `DecisionBody`(Strict) :84
- class `RollbackBody`(Strict) :88
- class `ScriptLab` :93 — script_settings:96, script_history:99, adopt_script_in:105, script_texts:129, testing_proposal:146, _running_test:162, _save_proposals:172, _proposal:175, _replace_proposal:183, _candidate:189, add_proposal_in:199, start_script_test_in:229, _stop_study:287, settle_script_tests:296, _memory_rows:374, latest_memory:385, _tests_view:389, write_memory_in:411, publish_script_learning:477, script_memory_digest_in:485, script_learning_context:491, _proposal_request:507, propose_from_memory:541, _accept_generated:588, script_lab_view:613
- `register_script_lab()` :640

### `optimization/script_learning.py` (506 lines, 4,143 nodes)

How prospects react to each part of a campaign's script, and what that says about closing.

- `tokens()` :39
- `variant()` :43
- `section_texts()` :48
- `section_label()` :67
- `match_section()` :73
- `_number()` :86
- `script_trace()` :90
- `_bucket()` :196
- `trace_features()` :203
- `feature_label()` :238
- `script_report()` :258
- `weakest_section()` :351
- `candidate_template()` :363
- `memory_digest()` :406
- `_pct()` :424
- `_finding()` :428
- `learned_markdown()` :441
- `compose_memory()` :502

### `optimization/workflows.py` (1,359 lines, 9,943 nodes)

- class `TrainingRows`(list) :33
- `dataset_checksum()` :39
- `next_attempts()` :55
- class `OptimizationWorkflows`(ComparisonWorkflows) :106 — selected_campaign:107, ensure_experiment_cohort:120, model_source_current:171, serving_model:190, reconcile_goal_status:273, schedule_optimization:288, optimization_config:327, health_policy_in:340, inventory_evidence:360, inventory_capacity:369, predicted_work_seconds:437, obligation_ends:444, selection_profile:473, operational_dataset:489, forecast_input:626, forecast:804, build_forecast:808, report_details:906, saved_forecast:979, optimization_report:1002, save_operational_dataset:1042, train_operational:1111, refresh_observed_health:1161, refresh_learning:1222, refresh_optimization:1276

### `postcall/__init__.py` (0 lines, 1 nodes)

- (constants only)

### `postcall/analysis.py` (269 lines, 1,751 nodes)

- `reviewed_tracker_matches()` :1
- `score_summary()` :17
- `behavioral_metrics()` :36
- `learning_observations()` :114
- `suggested_outcome()` :141
- `build_analysis()` :164

### `postcall/commitments.py` (189 lines, 1,460 nodes)

Reconcile source-linked local commitments in the analysis transaction.

- `identity_of()` :12
- `publish()` :16
- `human_retracted()` :145
- `invalidate()` :156

### `postcall/dates.py` (232 lines, 1,569 nodes)

Check extracted callback times without inventing appointments.

- `_zone()` :70
- `_weekdays()` :99
- `_date()` :103
- `_time()` :152
- `resolve_time()` :172

### `postcall/finalize.py` (230 lines, 1,432 nodes)

One post-call finalization: its job identity, its read-only input and its status.

- class `UnknownJobKind`(ValueError) :17
- `enqueue()` :21
- `unprocessed()` :48
- `status_in()` :58
- `request()` :80

### `postcall/review.py` (181 lines, 1,457 nodes)

Append-only human score reviews; the model assessment is never overwritten.

- class `ScoreReview`(Strict) :19 — consistent:31
- `reviewed_view()` :50
- `bind_api()` :85

### `postcall/rubrics.py` (362 lines, 1,273 nodes)

Versioned, seller-editable scoring anchors. These do not constrain live advice.

- class `Strict`(BaseModel) :21
- class `Dimension`(Strict) :25 — all_anchors:32
- class `Rubric`(Strict) :38 — unique_dimensions:44
- class `RubricConfig`(Strict) :50 — all_types:57
- class `PurposeEdit`(Strict) :63
- `dimension()` :69
- `defaults()` :78
- `configuration()` :255
- `snapshot()` :267
- `effective()` :286
- `bind_api()` :300

### `preparation/__init__.py` (0 lines, 1 nodes)

- (constants only)

### `preparation/templates.py` (245 lines, 1,515 nodes)

Seller-owned script structure with small, evidence-backed model-filled slots.

- class `ScriptSection`(BaseModel) :17
- class `ScriptTemplate`(BaseModel) :23 — placeholders:44
- class `PreparationPolicy`(BaseModel) :64 — valid_zone:78
- `template_version()` :86
- `policy_values()` :94
- `in_window()` :109
- `render()` :115
- `approved_template()` :141
- `fill()` :153
- `import_markdown()` :210

### `preparation/workflows.py` (610 lines, 4,588 nodes)

Advance preparation using the existing queue and bounded, versioned lead packets.

- `digest()` :21
- `failed_attempts()` :25
- `retry_due()` :32
- `needs_preparation()` :38
- `failure_state()` :42
- `preparation_runway()` :60
- class `Preparation` :93 — preparation_context:94, preparation_settings:118, script_base:121, lead_script_base:129, preparation_key:132, claim_preparation:155, release_preparation:181, publish_script_in:198, preparation_state:237, script_evidence:245, script_row:265, selected_script:276, preparation_candidates:334, pending_preparation:383, preparation_status_in:399, preparation_status:528, preparation_plan:532, prepare_ahead:545, queue_preparation_in:577

### `priority.py` (56 lines, 216 nodes)

Calling work runs ahead of background work inside one authority process.

- class `ForegroundGate` :18 — __init__:19, work:24, wait_idle:35
- `yield_to_calling_work()` :44
- `settle_heap()` :50

### `privacy.py` (797 lines, 4,517 nodes)

Small process-local raw-content cache. Persistent records contain only metadata.

- `usage_metadata()` :21
- class `_Entry`(NamedTuple) :61
- class `EphemeralBuffer` :71 — __init__:78, _remove:102, _purge:114, put:131, get:174, remaining_ms:180, has:192, discard:199, purge:203, clear:207, revoke_scope:217
- `event_metadata()` :226
- `bus_metadata()` :271
- `job_metadata()` :289
- `paraphrased()` :351
- `script_section_of()` :365
- `persistent_document()` :375
- class `SpeechPrivacy` :478 — raw_key:479, speech_revoked:482, transcript_metadata:500, transcript_rows:516, raw_status:542, _accept_transcript:558, _transcript_in:721, _publish_transcripts:777, _receive_transcript:782, transcript:791

### `providers/__init__.py` (0 lines, 1 nodes)

- (constants only)

### `providers/dnc.py` (72 lines, 424 nodes)

Documented prepaid lookup. A query result never replaces seller registry authority.

- class `DNCError`(ValueError) :16 — __init__:17
- class `PrepaidDNC` :22 — __init__:23, check:27

### `providers/inference.py` (259 lines, 1,709 nodes)

- class `InferenceError`(RuntimeError) :7
- class `InferenceResult` :12
- class `InferenceClient` :19 — __init__:20, search:42, complete:110, action_result:228

### `providers/oauth.py` (208 lines, 1,319 nodes)

- class `OAuthError`(ValueError) :11
- class `PlanOAuth` :15 — __init__:19, begin:23, identity:67, finish:92, refresh:145
- class `RegisteredPlanClient` :180 — __init__:184, complete:190

### `providers/openrouter.py` (436 lines, 2,469 nodes)

A dedicated, bounded Cerebras route; no provider or model fallback.

- class `RouteNotVerified`(InferenceError) :33 — __init__:36
- `wire_schema()` :41
- `provider_identity()` :65
- `is_cerebras()` :82
- `unique_fields()` :86
- `completed_fields()` :95
- `primary_candidate()` :128
- `truncated_turn()` :140
- class `OpenRouterCoach` :154 — __init__:155, _send:163, _complete:260, suggest:380, review:429

### `providers/simulated.py` (75 lines, 411 nodes)

- class `UncertainCarrierResult`(TimeoutError) :4
- class `CarrierNotSubmitted`(RuntimeError) :8
- class `SimulatedCarrier` :12 — __init__:15, dial:25, reconcile:35, hangup:41, playback:48, action:52, token:56, list_numbers:59, configure_number:62, get_number:66, search_numbers:69

### `providers/telnyx.py` (226 lines, 1,314 nodes)

- class `CarrierConfigurationError`(ValueError) :12
- `verify_webhook()` :16
- class `TelnyxCarrier` :28 — __init__:31, request:36, dial:58, token:85, hangup:96, action:99, playback:108, reconcile:111, list_numbers:129, search_numbers:158, configure_number:172, get_number:187, verify_incoming_only_credential:192

### `rep_workflows.py` (1,220 lines, 7,889 nodes)

Local commitments and rep-reported outcomes. No external scheduling effects.

- `zoned_time()` :32
- class `DatedEdit`(BaseModel) :41 — validate_time:50, validate_zone:55
- class `TaskEdit`(DatedEdit) :65 — validate_reminder:78, record_offset:83, status_cannot_be_null:96
- class `MeetingEdit`(DatedEdit) :102 — validate_callback:111
- class `OutreachEdit`(DatedEdit) :115
- class `RepWorkflows` :121 — local_edit_in:122, local_edit_receipt:159, check_edit_version:184, check_prospect_review:195, resolve_goal_review:205, prospect_context_in:248, lead_records:389, refresh_packet_in:411, queue_packet_refresh_in:418, refresh_continuity:435, refresh_conversation_features_in:448, _sync_callback:500, _callback_by_key:540, _callback_task:551, edit_task:640, _meeting_in:738, report_meeting:845, auto_disposition:883, save_disposition:928

### `reporting.py` (318 lines, 2,141 nodes)

Saved report views: computed in the background, served from their last publication.

- `collect_report_changes()` :77
- `publish_report_changes()` :95
- `home_key()` :117
- `projection_status()` :122
- class `ReportProjections` :141 — report_revision:142, report_scope:149, _saved_view:178, request_report:190, _publish_view:211, cached_home_metrics:233, rebuild_home_metrics:245, cached_optimization_report:268, rebuild_optimization_report:284, request_optimization_refresh:307, refresh_optimization_job:313

### `research/__init__.py` (0 lines, 1 nodes)

- (constants only)

### `research/fetch.py` (144 lines, 1,005 nodes)

- class `UnsafeURL`(ValueError) :11
- `validate_url()` :15
- `same_site()` :27
- `parse_page()` :33
- class `SafeFetcher` :48 — __init__:49, fetch:57, _fetch:64
- `same_origin()` :96
- `official_links()` :111
- `discovery_links()` :124

### `research/validation.py` (90 lines, 482 nodes)

- class `ValidationFailure`(ValueError) :6
- `script_review_request()` :22
- `validate_script_evidence()` :54
- `validate_script()` :82

### `runtime.py` (121 lines, 754 nodes)

One bounded lane runner, shared by the bundled authority and background process.

- class `AuthorityRuntime` :22 — __init__:23, _committed:32, _idle_seconds:41, _lane:56, start:79, close:89
- `authority_lifetime()` :101

### `scheduling/__init__.py` (0 lines, 1 nodes)

- (constants only)

### `scheduling/models.py` (440 lines, 3,038 nodes)

Shared observed labels and inexpensive sparse estimates; no ML runtime imports.

- class `EstimatePolicy`(BaseModel) :78 — version:87
- `prediction_key()` :96
- `company_group()` :109
- `profile_current()` :118
- `saved_prediction()` :129
- `derive_labels()` :169
- `contact_labels()` :197
- `wilson()` :202
- `_finite()` :212
- `_instant()` :221
- `attempt_work_seconds()` :229
- `_cell()` :254
- `_weighted_mean()` :283
- `_stage_counts()` :290
- `estimate_rows()` :312
- `sum_counts()` :324
- `estimate_counts()` :332
- `prediction_from_counts()` :361
- `pooled_predictions()` :421

### `scheduling/policy.py` (39 lines, 233 nodes)

- `parsed()` :6
- `queue_decision()` :10
- `rank_candidates()` :22

### `scheduling/queue.py` (557 lines, 3,726 nodes)

Bounded queue reads, batched ranking, and a short authoritative resource claim.

- `decision_state()` :34
- class `SelectionChanged`(Exception) :42
- class `ReservationPlanner` :46 — _hold_in_flight:47, _number_holds:62, _release_in_flight:78, _queue_context:86, _queue_presence:133, _selection:144, _selection_page:169, reserve:363, _claim_in:388

### `security.py` (165 lines, 972 nodes)

- class `LazyPasswordHash` :14 — __init__:17, _get:20, hash:28, verify:31
- class `ConfigStore` :38 — __init__:45, read:50, write:56
- `_token_hash()` :87
- `issue_session()` :91
- `decode_session()` :120
- `admin()` :163

### `service.py` (1,179 lines, 8,283 nodes)

- `dt()` :28
- `fail()` :32
- class `Service`(ReservationPlanner) :36 — __init__:37, required:68, scoped:80, _by_key:83, context_rows:93, carrier:113, setup:126, seed:231, suppressed:357, import_leads:374, leads:464, budget_ok:471, context:509, record_decision:603, agent_leg_in:619, release_unused_reservation_in:639, presence:654, _expirable:672, _expired_claims:677, _expire_claims:681, agent_ready_in:694, _prepare_call:706, _call_snapshot:825, start_call:829, _claim_dial:836, _dial_failed:902, _dial_completed:929, dispatch_call:952, bind_dial_receipt:971, finish_call:1005, _claim_hangup:1031, _hangup_completed:1090, hangup:1114, suppress:1130, event:1135

### `telephony/__init__.py` (0 lines, 1 nodes)

- (constants only)

### `telephony/callbacks.py` (747 lines, 5,328 nodes)

Bounded inbound transfers with an honest message/task fallback, using the existing outbox.

- `valid_destination()` :28
- class `CallbackRoutingEdit`(BaseModel) :45 — destination_valid:54
- class `InboundCallbacks` :58 — callback_config:59, read_callback_routing:63, edit_callback_routing:72, _inbound_command:100, _release_inbound_claim:115, _route_inbound:119, _receive_inbound:224, _task_commands:315, _orphan_unreported:328, _inbound_finished:338, inbound_event:346, end_inbound:523, reconcile_inbound:547, inbound_watchdog:597, inbound_admit_command:644

### `telephony/ingress.py` (339 lines, 2,464 nodes)

Provider receipt, retry and completion share one identity. Raw speech is ephemeral.

- class `WebhookIngress` :21 — accept_webhook:22, _receive_webhook:30, process_webhook:97, _process_webhook:100, _finish_webhook:162, _webhook_call:175, _transcription_call:205, _agent_transcript_in:227, _apply_webhook_in:247, queue_unfinished_webhooks:293

### `telephony/recovery.py` (437 lines, 3,141 nodes)

Read-only carrier reconciliation before any retry of an uncertain effect.

- `safe_stop()` :27
- `within_command_window()` :31
- `call_receipt()` :35
- class `CommandRecovery` :52 — _claim_reconciliation:53, _complete_reconciliation:87, reconcile_command:185, _recovery_snapshot:202, dial_evidence_expired:274, dial_unconfirmed_in:283, finish_terminal_in:319, _finish_orphan:331, _expired_agents:343, recover:384

### `telephony/recovery_actions.py` (391 lines, 2,779 nodes)

Operator actions over existing intents and jobs; never manufacture a replacement dial.

- `error_code()` :19
- class `RecoveryActions` :25 — _job_visible:26, _job_retry_reason:47, recovery_items:65, _confirm_not_placed:200, _recovery_action:213, recovery_action:269, retry_job:289, reconcile_agent:321

### `telephony/reducer.py` (66 lines, 199 nodes)

- `owns_effect()` :29
- `reduce_state()` :62

### `telephony/workflows.py` (669 lines, 4,814 nodes)

- class `TelephonyWorkflows` :19 — _keypad_intent:20, keypad_status:51, send_keypad:66, control_intent:71, event:97, start_transcription_in:275, stop_agent_transcription_in:297, finish_call:315, _control_work:322, _claim_control:356, _control_completed:447, dispatch_controls:474, _simulated_agent:499, _agent_context:518, _claim_agent:537, _agent_failed:564, _agent_completed:573, verified_credential:597, create_agent:605, agent_event:634

### `transcripts.py` (46 lines, 222 nodes)

- `normalize_transcription()` :10

### `workspace.py` (333 lines, 2,741 nodes)

Bounded workspace previews and resumable encrypted history; no new search store.

- `scoped_query()` :29
- `after()` :33
- class `WorkspaceReads` :37 — quiescence_in:38, workspace_page:82, _mark_suppressed:174, _label_leads:189, _job_page:210, snapshot:230

## Tests

- `services/backend/tests/acceptance/test_auto_wrapup.py` — 7 test functions
- `services/backend/tests/acceptance/test_calling_preferences.py` — 4 test functions
- `services/backend/tests/acceptance/test_feature_calling.py` — 22 test functions
- `services/backend/tests/acceptance/test_feature_compliance.py` — 13 test functions
- `services/backend/tests/acceptance/test_feature_learning.py` — 6 test functions
- `services/backend/tests/acceptance/test_feature_live_helper.py` — 9 test functions
- `services/backend/tests/acceptance/test_feature_models.py` — 8 test functions
- `services/backend/tests/acceptance/test_feature_operations.py` — 15 test functions
- `services/backend/tests/acceptance/test_feature_postcall.py` — 8 test functions
- `services/backend/tests/acceptance/test_feature_preparation.py` — 7 test functions
- `services/backend/tests/acceptance/test_feature_script_learning.py` — 12 test functions
- `services/backend/tests/acceptance/test_feature_smoke.py` — 1 test functions
- `services/backend/tests/acceptance/test_feature_workspace.py` — 10 test functions
- `services/backend/tests/acceptance/test_simple_script.py` — 2 test functions
- `services/backend/tests/test_action_tools.py` — 5 test functions
- `services/backend/tests/test_adaptive_capacity.py` — 1 test functions
- `services/backend/tests/test_adaptive_model.py` — 2 test functions
- `services/backend/tests/test_capacity_conservation.py` — 5 test functions
- `services/backend/tests/test_coach_context_interpretation.py` — 3 test functions
- `services/backend/tests/test_coach_replay.py` — 2 test functions
- `services/backend/tests/test_coach_stream.py` — 10 test functions
- `services/backend/tests/test_conversation_associations.py` — 6 test functions
- `services/backend/tests/test_conversation_timing.py` — 8 test functions
- `services/backend/tests/test_desktop_launch.py` — 3 test functions
- `services/backend/tests/test_grounding_review.py` — 11 test functions
- `services/backend/tests/test_number_provider_paths.py` — 1 test functions
- `services/backend/tests/test_oauth.py` — 4 test functions
- `services/backend/tests/test_openrouter_coach.py` — 13 test functions
- `services/backend/tests/test_optimization_numbers.py` — 18 test functions
- `services/backend/tests/test_planner_correctness.py` — 15 test functions
- `services/backend/tests/test_policy.py` — 5 test functions
- `services/backend/tests/test_providers.py` — 8 test functions
- `services/backend/tests/test_pure_serving.py` — 6 test functions
- `services/backend/tests/test_remediation_semantics.py` — 3 test functions
- `services/backend/tests/test_research_fetch.py` — 2 test functions
- `services/backend/tests/test_script_grounding.py` — 7 test functions
- `services/backend/tests/test_script_learning.py` — 10 test functions
- `services/backend/tests/test_sqlite_plans.py` — 1 test functions
- `services/backend/tests/test_store_format.py` — 1 test functions
- `services/backend/tests/test_strategy_policy.py` — 4 test functions
- `services/backend/tests/test_telnyx_contracts.py` — 24 test functions
- `services/backend/tests/test_time_capacity.py` — 25 test functions
- `services/backend/tests/test_training_isolation.py` — 19 test functions
- `services/backend/tests/test_unified_planning.py` — 40 test functions
- `tests/test_disposable_database.py` — 12 test functions
- `tests/test_distribution_contract.py` — 14 test functions
- `tests/test_local_ingress_proxy.py` — 2 test functions
- `tests/test_package_macos.py` — 6 test functions

## Frontend (`apps/desktop/src`)

- `App.tsx` (942 lines): Brand, App
- `api/client.ts` (238 lines): ApiError, localEdit, canUseOfflineCache, native, ProviderCredential, assertNoPendingOptouts, TunnelConfig, TunnelStatus, tunnelStatus, saveTunnel, localRuntime, waitForRuntime, REQUEST_TIMEOUT_MS, LONG_REQUEST_MS, RequestOptions, request, SavedSession, saveSession, loadSession, clearSession, openSource, validateServer, readCache, writeCache
- `api/live.ts` (182 lines): LiveEvent, liveUrl, LiveConnection
- `components/AdminWorkflows.tsx` (111 lines): AccessAndRecovery
- `components/AdvancedIntelligence.tsx` (126 lines): Trackers
- `components/BillingReconciliation.tsx` (471 lines): BillingReconciliation
- `components/CallControls.tsx` (240 lines): CallControls
- `components/CallbackRouting.tsx` (147 lines): CallbackRouting
- `components/Coaching.tsx` (455 lines): Coaching, ReviewTip
- `components/ConversationLearning.tsx` (195 lines): ConversationLearning
- `components/ConversationObservations.tsx` (77 lines): ConversationObservations
- `components/ConversionForm.tsx` (133 lines): ConversionForm
- `components/CredentialField.tsx` (58 lines): CredentialField
- `components/DialerPage.tsx` (1018 lines): DialerPage
- `components/DncScreeningPanel.tsx` (199 lines): DncScreeningPanel
- `components/FollowupTasks.tsx` (345 lines): FollowupTasks, DueTasks
- `components/HistoryMore.tsx` (36 lines): HistoryMore
- `components/InboundCall.tsx` (91 lines): InboundCall
- `components/Keypad.tsx` (233 lines): Keypad
- `components/LiveCoach.tsx` (407 lines): LiveCoach
- `components/OperatingPolicySettings.tsx` (615 lines): OperatingPolicySettings
- `components/Operations.tsx` (1712 lines): LeadsPage, NumbersPage, IntelligencePage, Empty
- `components/OptimizationPanel.tsx` (630 lines): HealthPolicy, OptimizationSettings, OptimizationReport, useOptimizationReport, ReportState, OptimizationPanel, LearningAdvanced
- `components/Preparation.tsx` (313 lines): PreparationRunway, PreparationSettings
- `components/ProductKnowledge.tsx` (166 lines): ProductKnowledge
- `components/ProspectBrief.tsx` (415 lines): ProspectBrief
- `components/RecoveryPanel.tsx` (276 lines): RecoveryPanel
- `components/ReviewPage.tsx` (571 lines): ReviewPage
- `components/Rubrics.tsx` (333 lines): RubricSettings, CallPurpose
- `components/ScoreReview.tsx` (220 lines): ScoreReview
- `components/ScriptMemory.tsx` (563 lines): Markdown, ScriptMemory
- `components/ScriptReader.tsx` (41 lines): ScriptReader
- `components/ScriptUpload.tsx` (141 lines): ScriptUpload
- `components/SettingsPage.tsx` (2090 lines): SettingsPage
- `components/Staffing.tsx` (321 lines): StaffedWindow, SimpleStaffing, ZonePicker, WeeklySchedule, DatedStaffing
- `components/TrialControls.tsx` (647 lines): ControlledComparison, Experiments
- `components/WorkTime.tsx` (223 lines): WorkTime, CallingHealth
- `components/WrapUp.tsx` (267 lines): WrapUp, CallbackReviewNotice
- `context.tsx` (62 lines): WorkspaceContext, Context, useWorkspace, label, date, percent, isAdmin
- `main.tsx` (9 lines):
- `state/call-shortcuts.ts` (32 lines): canAdvanceWithSpace
- `state/csv.ts` (49 lines): parseCSV
- `state/definitions.ts` (60 lines): FieldBound, Definitions, useDefinitions, limits, options
- `state/optouts.ts` (49 lines): StorageLike, OptOut, OptOutJournal
- `state/privacy.ts` (175 lines): persistentCache, KEYPAD_REPLAY_MS, boundKeypad, receiveSpeech, expireSpeech, speechDeadline, ExpiryClock, watchSpeechExpiry
- `state/queue.ts` (42 lines): nextLeadWithRetry
- `state/refresh.ts` (27 lines): coalesceRefresh
- `state/useHistory.ts` (96 lines): countLabel, useHistory
- `state/useLoad.ts` (28 lines): useLoad
- `state/useSpeechExpiry.ts` (23 lines): useSpeechExpiry
- `state/useWorkHeartbeat.ts` (67 lines): useWorkHeartbeat
- `state/worktime.ts` (31 lines): heartbeatActivity
- `state/zones.ts` (80 lines): deviceZone, chooseZone, zoneCaption, zonedInput, zonedInstant, localTime
- `telephony/client.ts` (306 lines): pausesPresence, Telephony
- `types.ts` (683 lines): Session, Runtime, Lead, FollowupTask, NumberRecord, Segment, Coach, Call, Script, Fact, Packet, Reservation, CapabilityStatus, Readiness, Policy, Playbook, HistoryPage, Workspace, EstimatePolicy, EvidenceItem, Analysis, isTerminal, isActiveCall

## Desktop shell (`apps/desktop/src-tauri/src`)

- `lib.rs` (967 lines). Tauri commands: webhook_tunnel_status, webhook_tunnel_set, backend_config, open_source. Functions: current, take, forget_exited, validate_tunnel, webhook_ssh_args, tunnel_retry_delay, new_group, stop, drop, spawn_webhook_ssh, start, stop, drop, stop, stop_for, load_tunnel_config, save_tunnel_config, write_private_file, local_backend_healthy, sync_tunnel, webhook_tunnel_status, webhook_tunnel_set, drop, register_exit_cleanup, managed_sidecar, blocking, backend_config, start_backend, launch_secrets, drain_backend, open_source, stop_owned_runtimes, wait_for_backend_release, run, temp_root, fixture, webhook_tunnel_owned_runtime_lifecycle, webhook_tunnel_rejects_argument_injection_and_nonmanaged_destination, webhook_tunnel_missing_ssh_is_an_actionable_state, webhook_supervisor_reaps_stale_child_and_can_be_stopped_during_backoff, webhook_tunnel_stop_also_terminates_ssh_helpers_in_its_owned_group
- `main.rs` (4 lines). Tauri commands: . Functions: main

## Scripts (`scripts`)

- `backend_entry.py`
- `bench.py` — Speed bench: hot calling paths on a populated store (simulated carrier, no paid calls).
- `build_sidecar.py` — Freeze the local authority and its separate CPU trainer into self-contained sidecars.
- `disposable_database.py` — Destructive test-store admission. This module is never packaged in the app.
- `e2e_server.py` — Ephemeral real-HTTP authorities for Playwright; no access to installed app data.
- `evaluate_coach.py` — Evaluate the dedicated OpenRouter Cerebras route without placing calls or uploading recordings.
- `package_macos.py` — Seal the Python sidecars with their own entitlement, then sign the final update bytes.
- `release_manifest.py` — Collect verified native artifacts and create one versioned multi-platform update manifest.
- `size.py` — Codebase size: lines per area plus Python AST nodes and TypeScript tokens.
- `trainer_entry.py` — Frozen CPU trainer: the bounded training-child protocol and nothing else.
