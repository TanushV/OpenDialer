# OpenDialer implementation plan

Source: [complete specification](spec/IMPLEMENTATION_SPEC.md), preserved verbatim. Re-read the full source on every context compaction. Checkboxes mean implemented and verified by the stated software checks; external acceptance gates remain separate.

## Scope and operating modes

- [x] Read the full source specification and supplementary context; inspect the empty repository and tools.
- [x] Record user extension: fully isolated local operation as well as authenticated connected operation.
- [x] Record user constraint: no carrier/OpenAI/Groq API keys available; implement and test without them.
- [x] Write all initial domain, API, provider, recovery, UI, and installer acceptance tests before implementation; record an actual failing run.
- [ ] SQLite local authority, bundled backend and worker, loopback nonce, OS-protected credentials, persistent app data.
- [ ] PostgreSQL connected authority, scoped authentication, tenant/seller isolation, durable worker lanes.
- [ ] Explicit simulation workspace with no accidental network side effects; live configuration and readiness separate.

## A. Desktop, identity, telephony (§4–6,11,16–18)

- [ ] Tauri v2 + React/TypeScript app, restrictive native capabilities and CSP, microphone metadata.
- [ ] Setup wizard for local/connected modes; scoped sessions, roles, credential configuration and health preflight.
- [ ] Telnyx WebRTC adapter, devices, incoming agent leg, mute, keypad, reconnect, readiness, pause and emergency hangup.
- [ ] Server-controlled linked agent/prospect legs with explicit caller ID, bridge guard, max lifetime and deterministic IDs.
- [ ] Signed webhook ingress, durable event inbox, monotonic reducer, out-of-order/duplicate protection.
- [ ] Durable command outbox, uncertain-response reconciliation, fencing, recovery sweep and orphan cleanup.
- [ ] Rep calling workspace: lead packet, local time, script, one live coach card, transcript, call controls and wrap-up.
- [ ] Callback routing, ambiguity handling, teammate/queue fallback, retained inbound number service.

## B. Safe operations and numbers (§6–7,9–10,18)

- [ ] Schema, migrations, UTC/UUID identity, DB uniqueness, indexes and audit history.
- [ ] CSV/JSON lead imports, normalization, separate format/ownership/reachability/permission states and import report.
- [ ] Authoritative seller-specific national/state registry staging/publication, checksum/freshness/coverage and change lists.
- [ ] Internal suppressions survive imports/restores; local offline opt-out journal blocks dialing until synchronized.
- [ ] Pre-command policy recheck: identity, suppression, registry, consent, timezone, cadence, rep, number, route and budget.
- [ ] Separate live/prerecorded/transcription/recording/messaging permissions; selective RND evidence adapter.
- [ ] Atomic lead/endpoint/rep claims, number capacity leases, expiry handling and cross-rep concurrency tests.
- [ ] Stable lead-number bindings; regional/general fallback, ownership isolation and unhealthy number pause.
- [ ] Existing-number synchronization, demand-based procurement proposal, approved budget, Telnyx purchase/setup adapters.
- [ ] Registration checklist/evidence, inbound callback tests, legitimate warmup progression and number-health monitoring.

## C. Preparation and inference (§8,16,19)

- [ ] OpenAI Responses adapter, configurable model roles, schema/refusal/deadline handling, usage ledger.
- [ ] Explicit OpenAI-compatible LiteLLM adapter with configurable endpoint and credentials.
- [ ] Official eligible ChatGPT-plan adapter: public Responses, stream=true/store=false, terminal-event checks; optional OAuth registration/sign-in.
- [ ] OpenAI Batch submission/result reconciliation by custom_id, partial retry and no Batch assumption for plan usage.
- [ ] Bounded multi-lead preparation, company caching/single-flight, priorities, retries/dead-letter and independent outcomes.
- [ ] Safe fetcher with DNS/redirect/size/content/port restrictions; wrong-entity, stale evidence and prompt-injection rejection.
- [ ] Source/fact provenance, approved playbooks, script writer/modifier versions/diffs, deterministic claims validation and fallback.
- [ ] Hard provider/job/search/token/procurement budgets and per-attempt native accounting.

## D. Speech, review and intelligence (§13,15)

- [ ] Telnyx Soniox/Parakeet transcription adapter, permission gates and side/revision/finality lineage.
- [ ] Conversation state versions, stable-turn updates, opt-out safety trigger outside the model.
- [ ] Quick and final post-call analysis; evidence-linked problems, objections, commitments, unknowns and proposed CRM updates.
- [ ] Human notes preserved across late AI revisions with explicit conflict/diff handling.
- [ ] Review workspace with evidence, permitted recording, timeline, corrections, comments, disputed/low-confidence filters.
- [ ] Keyword/semantic trackers, versioned observable rubrics, N/A/insufficient evidence, score coverage and human appeals.
- [ ] Talk/turn/overlap metrics with missing-data denominators; no emotion/personality inference.
- [ ] Cross-call memory, commitments/follow-up tasks, reviewed snippet library and team associations.

## E. Live coaching (§14)

- [ ] Groq strict JSON adapter, bounded context, approved retrieval and no live web search.
- [ ] Useful triggers, NO_ACTION, one request/card per call, version/TTL/cooldown enforcement and cancellation.
- [ ] Grounding and claims validation, static fallback, deadline/circuit breaker and service status.
- [ ] Replay evaluation harness and measured software latency; reproducible 20B/120B benchmark command.

## F. Scheduling, models and measurement (§9–10,19,22)

- [ ] Callback/requested-contact/coverage/ranked priorities, conservative time windows and visible capacity shortfall.
- [ ] Eligible-only feature snapshots without registry/sensitive/future information.
- [ ] Smoothed/pooled baseline and calibrated gradient-boosted challenger, chronological grouped evaluation and model artifacts.
- [ ] Constrained experimentation, stable control, exact action propensities, delayed/censored rewards and manual promotion/rollback.
- [ ] Cohort denominator, unique right-party reach, meetings, rep time, coverage, opt-outs, costs and billing reconciliation.

## G. Voicemail and recovery (§11–12,18)

- [ ] Monitored drop with separate permission, actual mailbox/beep readiness, approved assets and opt-out handling.
- [ ] Independently durable detached workflow gated on topology proof, playback-start confirmation and late-human recovery.
- [ ] No-beep/full-mailbox/false-machine/playback-failure/watchdog and backend-restart tests.
- [ ] Human review for advanced automation; optional later neural/acoustic models remain outside activation until accepted.

## Delivery, validation and cleanliness (§3,17–24)

- [ ] Full test matrix in SQLite and real PostgreSQL, API integration/contract/replay/recovery and frontend end-to-end tests.
- [ ] Docker/Caddy deployment, separate worker priorities, migrations, encrypted backup/restore and audit-journal reconciliation.
- [ ] Installable self-contained desktop bundle, persistent app data and signed update infrastructure.
- [ ] CI for Python/TypeScript/Rust/security/build and cross-platform release packaging with lockfiles/licenses.
- [ ] Use native desktop app through computer use; exercise local and connected workflows and record evidence.
- [ ] Validate update replaces app in place and preserves data without uninstalling/reinstalling.
- [ ] Final preflight: tests/types/lint/build/security/secret scan, database checks, no tracked/untracked junk and clean git status.
- [ ] Frequent milestone commits pushed to TanushV/OpenDialer; deliver artifacts and honest acceptance report.

## External gates (cannot be certified without accounts/hardware/deployment)

- [ ] Telnyx traffic approval, master-key bypass attempt, caller-ID/bridge/AMD/voicemail behavior, live CDR billing and real callback call.
- [ ] Live Soniox/Parakeet attribution/finality/latency and Groq/OpenAI model availability/quotas/quality trials.
- [ ] Permissioned real-call review set, outcome trial, calibrated scorecards and proven cohort lift.
- [ ] Seller registry access and counsel-approved destination/recording/prerecorded policies.
- [ ] Windows/Linux hardware audio/NAT/Bluetooth/sleep/long-call certification and production VM/ARM64 capacity.
- [ ] Public distribution signing/notarization and configured public server/DNS/provider credentials.

User explicitly instructed continuing without unavailable APIs. External gates stay visible and associated features fail closed where required.
