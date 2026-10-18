# Pickup, conversion, allocation, and live-guidance completion

**October 17 correction:** [REMEDIATION_PLAN.md](REMEDIATION_PLAN.md) supersedes the completion claims and rigid coaching behavior below. All audit defects and missing workflows are open there. In particular, Cerebras must generate contextual freeform help, statistical learning must use trustworthy observed outcomes, and allocation must minimize useful additions to existing capacity. These historical checkboxes do not certify those repaired behaviors. The current turn authorizes full implementation of the revised plan. Read its latest scope first: proactive fixed-strip coaching, advance script batches, five-minute raw retention, all-US DNC, no subscriptions/calendar sync, final-attempt voicemail.

Accepted October 16, 2026. This is the implementation checklist for the user's instruction to write down and implement the complete follow-up proposal. It extends the preserved original specification and both local and connected modes. Re-read this file, the original specification, PLAN.md, and STATUS.md after every compaction.

## Outcome contract

Optimize qualified conversions per original eligible lead and per rep-hour, reporting human pickup and unique right-party reach separately. Default conversion is a held meeting; campaigns can select qualified conversation or won sale. Preserve imported, excluded, originally eligible, pending, and matured denominators. Never obtain apparent lift by hiding difficult leads, repeatedly contacting already reached leads, or relabeling screening/voicemail as human connections. Permissions, callbacks, coverage, identity, capacity, and budgets remain hard constraints.

## 0. TDD and records

- [x] Re-ingest original requirements, inspect the clean existing release, and record this extension.
- [x] Commit the complete initial acceptance tests before production changes; retain their actual red output.
- [x] Add a failing regression before each newly discovered defect; maintain meaningful domain, API, UI and recovery checks.
- [x] Update this checklist and STATUS.md at each milestone; commit and push coherent milestones frequently.

## 1. Number allocation and procurement

- [x] Forecast attempts by region/time horizon from eligibility, due retries, callbacks, campaign limits, available rep time and calling windows.
- [x] Subtract usable existing capacity, warmup limits, today's use, active claims and detached voicemail obligations before procurement.
- [x] Replace fixed number-health values with first-party observed delivery/answer/callback/short-call evidence, pooled case-mix adjustment, sample counts and uncertainty; unknown is explicit.
- [x] Make selection explainable: stable prospect binding, permitted regional/general fallback, capacity/load and evidence; audit every reassignment.
- [x] Review genuine limited use and advance or reduce bounded warmup limits; retain incoming callbacks during outbound pauses/retirement.
- [x] Produce fresh-quote purchase proposals inside approved budgets; preserve uncertain order intents across retries and restarts.
- [x] Evaluate regional allocation with stable cluster assignments, counts, conversion outcomes and uncertainty; never churn identity to evade enforcement.

## 2. Operational statistical learning

- [x] Capture immutable, versioned pre-decision features and the complete allowed action set/selection probability; use snapshots rather than mutable current lead fields for historical learning.
- [x] Derive human/right-party/qualified/meeting/sale labels consistently, preserving wrong party, screening, IVR, voicemail and unsuccessful attempts.
- [x] Attach idempotent later conversion events to leads/calls, retain multi-attempt lineage, and mature delayed windows without turning pending outcomes into failures.
- [x] Build authority-owned training snapshots automatically from operational records with seller isolation and no future/DNC/sensitive features.
- [x] Replace the narrow prediction input with local hour/weekday, attempts, elapsed time, evidence, source freshness, segment, callback history and measured health/missingness.
- [x] Estimate funnel probabilities, handling time and observed carrier costs; rank expected conversions per rep time subject to eligibility and coverage.
- [x] Generate future allowed slots and retries, prioritize requested callbacks and coverage, and expose infeasible capacity instead of postponing leads forever.
- [x] Learn approved treatment preferences from matured outcomes with shrinkage, fixed control, bounded exploration and exact logged propensities.
- [x] Queue bounded automatic retraining from immutable datasets; evaluate independent chronological/entity partitions against the deployed pooled baseline, not an unrelated constant.
- [x] Monitor calibration, drift, subgroup performance and coverage; require reviewed trial evidence before activation and support audited rollback.
- [x] Provide controlled-trial reports with stable denominators, confidence intervals and sample sufficiency; software reports cannot manufacture a live lift claim.

## 3. Live guidance and Cerebras via OpenRouter

- [x] Add secure dedicated coach provider/model settings and an OpenRouter adapter defaulting to openai/gpt-oss-120b, pinned to Cerebras with provider fallbacks disabled and required parameter/JSON-schema support.
- [x] Bound reasoning/output/deadlines; handle refusals, invalid objects, rate limits, disconnects and cancellations; record actual model/provider/request/usage/cost/latency receipts.
- [x] Maintain versioned conversation state: stage, evidenced needs/impact, unresolved objections, answered questions, signals, commitments and earlier call context.
- [x] Retrieve relevant approved cards/claims/fresh lead facts by meaning/tags/stage instead of always selecting the existing-provider card.
- [x] Trigger useful opportunities, suppress repeats/declines/stale work and support manual help; deterministic opt-out/hangup controls remain independent.
- [x] Validate grounded meaning as well as IDs; restrict spoken suggestions to reviewed approved formulations and evidenced context, rejecting unsupported numerical/identity claims.
- [x] Deliver one short card at a useful pause with expiry/cancellation/static fallback and feedback; no model failure may block audio or calling controls.
- [x] Connect the desktop to authenticated scoped live events with cursor replay, deduplication, reconnect and session expiry; remove normal active-call polling.
- [x] Measure STT/state/retrieval/inference/delivery separately; initial p95 useful-card objective is 2.5 seconds after a qualifying utterance, explicitly unproven until live tests.
- [x] Provide reviewed replay evaluations and rep-balanced controlled comparisons measuring usefulness, grounding, timing, distraction and conversions.

## 4. Product, compatibility, and delivery

- [x] Expose goal/window/exploration/training settings, forecast/health/reassignment explanations, future slots, datasets/model/drift/trial reports, conversion recording and coaching quality/latency in the desktop.
- [x] Implement identical authority services for local SQLite and shared PostgreSQL, with scoped roles, encrypted persistence and protected provider secrets.
- [x] Migrate the existing store additively; preserve exact IDs, human notes, suppressions and protected session state.
- [x] Verify SQLite/PostgreSQL domain/API contracts, frontend unit/browser workflows, formatting/types/security/native checks and builds.
- [x] Build matching Mac ARM64 and Windows x64 updates; sign and verify artifacts, publish the shared update manifest, and update the existing Mac app in place.
- [x] Use native computer control to exercise updated local and connected workflows and record sanitized evidence; exercise installed Windows package/update continuity.
- [x] Finish with clean pushed Git state, coherent release/docs, intact database, no active sample claims, no task-owned orphan services and no credentials/build products in Git.

## Live acceptance gates (require unavailable account/data/hardware evidence)

- [ ] Permissioned Telnyx calls, callbacks, identity/bridge behavior, carrier records and hardware audio on both platforms.
- [ ] Funded OpenRouter/Cerebras requests verifying actual route, model quality, usage and complete STT-to-card latency under load.
- [ ] Reviewed playbooks/conversion definitions and real conversation replay set.
- [ ] Sufficient-volume controlled campaign trial proving pickup/conversion improvement with preserved coverage, cost and uncertainty.
- [ ] Configured production host/DNS/TLS and public publisher-trust credentials when required for distribution.

No live gate becomes checked because simulation or a mock returned success. Existing user instruction is to continue without unavailable API credentials; complete software and reproducible fixtures and leave these evidence gates explicit.

Implementation checks above are software contracts and synthetic/operational fixtures. Additive migration preservation is verified at schema level; the installed-app update remains a separate unchecked delivery gate. Timing instrumentation preserves unknown STT/full-path values.

## Documentation checked

- OpenRouter provider routing: https://openrouter.ai/docs/guides/routing/provider-selection
- Endpoint-specific JSON-schema support: https://openrouter.ai/docs/guides/features/structured-outputs
- Candidate endpoint metadata: https://openrouter.ai/api/v1/models/openai/gpt-oss-120b/endpoints
- Original Telnyx, Tauri, scheduling, consent and provider sources remain in spec/IMPLEMENTATION_SPEC.md.

## 0.2.1 upgrade-correctness regression gate

- [x] Reproduce the actual Windows 0.1.1→0.2.0 upgrade retaining an old backend; withdraw the affected candidate.
- [x] Write and run a failing cleanup-before-exit native test; add an exact-path PowerShell cleanup scope test.
- [x] Add resource-owned runtime cleanup for updater early exit and an NSIS legacy-backend preinstall hook without deleting workspace data.
- [x] Pass Windows cleanup scope, build both formats, fresh native workflow, and actual signed 0.1.1→0.2.1 upgrade with new backend version and preserved IDs/note/opt-out.
- [x] Build/verify Mac 0.2.1 and update the installed 0.2.0 app through its signed updater, preserving the current saved baseline.
- [x] Repeat final native preflight, publish accurate receipts, finish clean pushed Git state and scoped service cleanup.
