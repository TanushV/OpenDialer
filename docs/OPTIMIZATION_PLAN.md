# Pickup, conversion, allocation, and live-guidance completion

Accepted October 16, 2026. This is the implementation checklist for the user's instruction to write down and implement the complete follow-up proposal. It extends the preserved original specification and both local and connected modes. Re-read this file, the original specification, PLAN.md, and STATUS.md after every compaction.

## Outcome contract

Optimize qualified conversions per original eligible lead and per rep-hour, reporting human pickup and unique right-party reach separately. Default conversion is a held meeting; campaigns can select qualified conversation or won sale. Preserve imported, excluded, originally eligible, pending, and matured denominators. Never obtain apparent lift by hiding difficult leads, repeatedly contacting already reached leads, or relabeling screening/voicemail as human connections. Permissions, callbacks, coverage, identity, capacity, and budgets remain hard constraints.

## 0. TDD and records

- [x] Re-ingest original requirements, inspect the clean existing release, and record this extension.
- [x] Commit the complete initial acceptance tests before production changes; retain their actual red output.
- [ ] Add a failing regression before each newly discovered defect; maintain meaningful domain, API, UI and recovery checks.
- [ ] Update this checklist and STATUS.md at each milestone; commit and push coherent milestones frequently.

## 1. Number allocation and procurement

- [ ] Forecast attempts by region/time horizon from eligibility, due retries, callbacks, campaign limits, available rep time and calling windows.
- [ ] Subtract usable existing capacity, warmup limits, today's use, active claims and detached voicemail obligations before procurement.
- [ ] Replace fixed number-health values with first-party observed delivery/answer/callback/short-call evidence, pooled case-mix adjustment, sample counts and uncertainty; unknown is explicit.
- [ ] Make selection explainable: stable prospect binding, permitted regional/general fallback, capacity/load and evidence; audit every reassignment.
- [ ] Review genuine limited use and advance or reduce bounded warmup limits; retain incoming callbacks during outbound pauses/retirement.
- [ ] Produce fresh-quote purchase proposals inside approved budgets; preserve uncertain order intents across retries and restarts.
- [ ] Evaluate regional allocation with stable cluster assignments, counts, conversion outcomes and uncertainty; never churn identity to evade enforcement.

## 2. Operational statistical learning

- [ ] Capture immutable, versioned pre-decision features and the complete allowed action set/selection probability; use snapshots rather than mutable current lead fields for historical learning.
- [ ] Derive human/right-party/qualified/meeting/sale labels consistently, preserving wrong party, screening, IVR, voicemail and unsuccessful attempts.
- [ ] Attach idempotent later conversion events to leads/calls, retain multi-attempt lineage, and mature delayed windows without turning pending outcomes into failures.
- [ ] Build authority-owned training snapshots automatically from operational records with seller isolation and no future/DNC/sensitive features.
- [ ] Replace the narrow prediction input with local hour/weekday, attempts, elapsed time, evidence, source freshness, segment, callback history and measured health/missingness.
- [ ] Estimate funnel probabilities, handling time and observed carrier costs; rank expected conversions per rep time subject to eligibility and coverage.
- [ ] Generate future allowed slots and retries, prioritize requested callbacks and coverage, and expose infeasible capacity instead of postponing leads forever.
- [ ] Learn approved treatment preferences from matured outcomes with shrinkage, fixed control, bounded exploration and exact logged propensities.
- [ ] Queue bounded automatic retraining from immutable datasets; evaluate independent chronological/entity partitions against the deployed pooled baseline, not an unrelated constant.
- [ ] Monitor calibration, drift, subgroup performance and coverage; require reviewed trial evidence before activation and support audited rollback.
- [ ] Provide controlled-trial reports with stable denominators, confidence intervals and sample sufficiency; software reports cannot manufacture a live lift claim.

## 3. Live guidance and Cerebras via OpenRouter

- [ ] Add secure dedicated coach provider/model settings and an OpenRouter adapter defaulting to openai/gpt-oss-120b, pinned to Cerebras with provider fallbacks disabled and required parameter/JSON-schema support.
- [ ] Bound reasoning/output/deadlines; handle refusals, invalid objects, rate limits, disconnects and cancellations; record actual model/provider/request/usage/cost/latency receipts.
- [ ] Maintain versioned conversation state: stage, evidenced needs/impact, unresolved objections, answered questions, signals, commitments and earlier call context.
- [ ] Retrieve relevant approved cards/claims/fresh lead facts by meaning/tags/stage instead of always selecting the existing-provider card.
- [ ] Trigger useful opportunities, suppress repeats/declines/stale work and support manual help; deterministic opt-out/hangup controls remain independent.
- [ ] Validate grounded meaning as well as IDs; restrict spoken suggestions to reviewed approved formulations and evidenced context, rejecting unsupported numerical/identity claims.
- [ ] Deliver one short card at a useful pause with expiry/cancellation/static fallback and feedback; no model failure may block audio or calling controls.
- [ ] Connect the desktop to authenticated scoped live events with cursor replay, deduplication, reconnect and session expiry; remove normal active-call polling.
- [ ] Measure STT/state/retrieval/inference/delivery separately; initial p95 useful-card objective is 2.5 seconds after a qualifying utterance, explicitly unproven until live tests.
- [ ] Provide reviewed replay evaluations and rep-balanced controlled comparisons measuring usefulness, grounding, timing, distraction and conversions.

## 4. Product, compatibility, and delivery

- [ ] Expose goal/window/exploration/training settings, forecast/health/reassignment explanations, future slots, datasets/model/drift/trial reports, conversion recording and coaching quality/latency in the desktop.
- [ ] Implement identical authority services for local SQLite and shared PostgreSQL, with scoped roles, encrypted persistence and protected provider secrets.
- [ ] Migrate the existing store additively; preserve exact IDs, human notes, suppressions and protected session state.
- [ ] Verify SQLite/PostgreSQL domain/API contracts, frontend unit/browser workflows, formatting/types/security/native checks and builds.
- [ ] Build matching Mac ARM64 and Windows x64 updates; sign and verify artifacts, publish the shared update manifest, and update the existing Mac app in place.
- [ ] Use native computer control to exercise updated local and connected workflows and record sanitized evidence; exercise installed Windows package/update continuity.
- [ ] Finish with clean pushed Git state, coherent release/docs, intact database, no active sample claims, no task-owned orphan services and no credentials/build products in Git.

## Live acceptance gates (require unavailable account/data/hardware evidence)

- [ ] Permissioned Telnyx calls, callbacks, identity/bridge behavior, carrier records and hardware audio on both platforms.
- [ ] Funded OpenRouter/Cerebras requests verifying actual route, model quality, usage and complete STT-to-card latency under load.
- [ ] Reviewed playbooks/conversion definitions and real conversation replay set.
- [ ] Sufficient-volume controlled campaign trial proving pickup/conversion improvement with preserved coverage, cost and uncertainty.
- [ ] Configured production host/DNS/TLS and public publisher-trust credentials when required for distribution.

No live gate becomes checked because simulation or a mock returned success. Existing user instruction is to continue without unavailable API credentials; complete software and reproducible fixtures and leave these evidence gates explicit.

## Documentation checked

- OpenRouter provider routing: https://openrouter.ai/docs/guides/routing/provider-selection
- Endpoint-specific JSON-schema support: https://openrouter.ai/docs/guides/features/structured-outputs
- Candidate endpoint metadata: https://openrouter.ai/api/v1/models/openai/gpt-oss-120b/endpoints
- Original Telnyx, Tauri, scheduling, consent and provider sources remain in spec/IMPLEMENTATION_SPEC.md.
