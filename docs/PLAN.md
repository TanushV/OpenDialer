# OpenDialer implementation plan

Source: [complete specification](spec/IMPLEMENTATION_SPEC.md), preserved verbatim. Re-read the full source on every context compaction. Checkboxes mean implemented and verified by the stated software checks; external acceptance gates remain separate.

## Scope and operating modes

October 16 extension: [optimization completion plan](OPTIMIZATION_PLAN.md) is the active checklist for measured number health/demand, operational funnel learning, future scheduling, grounded live guidance and Cerebras through OpenRouter. The original checked items below describe the initial release, including its simplified baseline; they do not certify advanced optimization or live performance.

- [x] Read the full source specification and supplementary context; inspect the empty repository and tools.
- [x] Record user extension: fully isolated local operation as well as authenticated connected operation.
- [x] Record user constraint: no carrier/OpenAI/Groq API keys available; implement and test without them.
- [x] Write all initial domain, API, provider, recovery, UI, and installer acceptance tests before implementation; record an actual failing run.
- [x] SQLite local authority, bundled backend and worker, loopback nonce, OS-protected credentials, persistent app data.
- [x] PostgreSQL connected authority, scoped authentication, tenant/seller isolation, durable worker lanes.
- [x] Explicit simulation workspace with no accidental network side effects; live configuration and readiness separate.

## A. Desktop, identity, telephony (§4–6,11,16–18)

- [x] Tauri v2 + React/TypeScript app, restrictive native capabilities and CSP, microphone metadata.
- [x] Setup wizard for local/connected modes; scoped sessions, roles, credential configuration and health preflight.
- [x] Telnyx WebRTC adapter, devices, incoming agent leg, mute, keypad, reconnect, readiness, pause and emergency hangup.
- [x] Server-controlled linked agent/prospect legs with explicit caller ID, bridge guard, max lifetime and deterministic IDs.
- [x] Signed webhook ingress, durable event inbox, monotonic reducer, out-of-order/duplicate protection.
- [x] Durable command outbox, uncertain-response reconciliation, fencing, recovery sweep and orphan cleanup.
- [x] Rep calling workspace: lead packet, local time, script, one live coach card, transcript, call controls and wrap-up.
- [x] Callback routing, ambiguity handling, teammate/queue fallback, retained inbound number service.

## B. Safe operations and numbers (§6–7,9–10,18)

- [x] Schema, migrations, UTC/UUID identity, DB uniqueness, indexes and audit history.
- [x] CSV/JSON lead imports, normalization, separate format/ownership/reachability/permission states and import report.
- [x] Authoritative seller-specific national/state registry staging/publication, checksum/freshness/coverage and change lists.
- [x] Internal suppressions survive imports/restores; local offline opt-out journal blocks dialing until synchronized.
- [x] Pre-command policy recheck: identity, suppression, registry, consent, timezone, cadence, rep, number, route and budget.
- [x] Separate live/prerecorded/transcription/recording/messaging permissions; selective RND evidence adapter.
- [x] Atomic lead/endpoint/rep claims, number capacity leases, expiry handling and cross-rep concurrency tests.
- [x] Stable lead-number bindings; regional/general fallback, ownership isolation and unhealthy number pause.
- [x] Existing-number synchronization, demand-based procurement proposal, approved budget, Telnyx purchase/setup adapters.
- [x] Registration checklist/evidence, inbound callback tests, legitimate warmup progression and number-health monitoring.

## C. Preparation and inference (§8,16,19)

- [x] OpenAI Responses adapter, configurable model roles, schema/refusal/deadline handling, usage ledger.
- [x] Explicit OpenAI-compatible LiteLLM adapter with configurable endpoint and credentials.
- [x] Official eligible ChatGPT-plan adapter: public Responses, stream=true/store=false, terminal-event checks; optional OAuth registration/sign-in.
- [x] OpenAI Batch submission/result reconciliation by custom_id, partial retry and no Batch assumption for plan usage.
- [x] Bounded multi-lead preparation, company caching/single-flight, priorities, retries/dead-letter and independent outcomes.
- [x] Safe fetcher with DNS/redirect/size/content/port restrictions; wrong-entity, stale evidence and prompt-injection rejection.
- [x] Source/fact provenance, approved playbooks, script writer/modifier versions/diffs, deterministic claims validation and fallback.
- [x] Hard provider/job/search/token/procurement budgets and per-attempt native accounting.

## D. Speech, review and intelligence (§13,15)

- [x] Telnyx Soniox/Parakeet transcription adapter, permission gates and side/revision/finality lineage.
- [x] Conversation state versions, stable-turn updates, opt-out safety trigger outside the model.
- [x] Quick and final post-call analysis; evidence-linked problems, objections, commitments, unknowns and proposed CRM updates.
- [x] Human notes preserved across late AI revisions with explicit conflict/diff handling.
- [x] Review workspace with evidence, permitted recording, timeline, corrections, comments, disputed/low-confidence filters.
- [x] Keyword/semantic trackers, versioned observable rubrics, N/A/insufficient evidence, score coverage and human appeals.
- [x] Talk/turn/overlap metrics with missing-data denominators; no emotion/personality inference.
- [x] Cross-call memory, commitments/follow-up tasks, reviewed snippet library and team associations.

## E. Live coaching (§14)

- [x] Groq strict JSON adapter, bounded context, approved retrieval and no live web search.
- [x] Useful triggers, NO_ACTION, one request/card per call, version/TTL/cooldown enforcement and cancellation.
- [x] Grounding and claims validation, static fallback, deadline/circuit breaker and service status.
- [x] Replay evaluation harness and measured software latency; reproducible 20B/120B benchmark command.

## F. Scheduling, models and measurement (§9–10,19,22)

- [x] Callback/requested-contact/coverage/ranked priorities, conservative time windows and visible capacity shortfall.
- [x] Eligible-only feature snapshots without registry/sensitive/future information.
- [x] Smoothed/pooled baseline and calibrated gradient-boosted challenger, chronological grouped evaluation and model artifacts.
- [x] Constrained experimentation, stable control, exact action propensities, delayed/censored rewards and manual promotion/rollback.
- [x] Cohort denominator, unique right-party reach, meetings, rep time, coverage, opt-outs, costs and billing reconciliation.

## G. Voicemail and recovery (§11–12,18)

- [x] Monitored drop with separate permission, actual mailbox/beep readiness, approved assets and opt-out handling.
- [x] Independently durable detached workflow gated on topology proof, playback-start confirmation and late-human recovery.
- [x] No-beep/full-mailbox/false-machine/playback-failure/watchdog and backend-restart tests.
- [x] Human review for advanced automation; optional later neural/acoustic models remain outside activation until accepted.

## Mac and Windows delivery extension

- [x] Preserve the installed Mac identity; all subsequent delivery is an update, with no reinstall/reset.
- [x] Add Windows clean-install prerequisites: bundled WebView2 offline installer and IANA timezone data.
- [x] Build Windows NSIS/MSI installers with updater signatures and exercise the installed native app/restart in WebView2.
- [x] Publish matching Mac and Windows packages plus a shared signed-update manifest.

## Delivery, validation and cleanliness (§3,17–24)

- [x] Full test matrix in SQLite and real PostgreSQL, API integration/contract/replay/recovery and frontend end-to-end tests.
- [x] Docker/Caddy deployment, separate worker priorities, migrations, encrypted backup/restore and audit-journal reconciliation.
- [x] Installable self-contained desktop bundle, persistent app data and signed update infrastructure.
- [x] CI for Python/TypeScript/Rust/security/build and cross-platform release packaging with lockfiles/licenses.
- [x] Use native desktop app through computer use; exercise local and connected workflows and record evidence.
- [x] Validate update replaces app in place and preserves data without uninstalling/reinstalling.
- [x] Final preflight: tests/types/lint/build/security/secret scan, database checks, no tracked/untracked junk and clean git status.
- [x] Frequent milestone commits pushed to TanushV/OpenDialer; deliver artifacts and honest acceptance report.


## External gates (cannot be certified without accounts/hardware/deployment)

- [ ] Telnyx traffic approval, master-key bypass attempt, caller-ID/bridge/AMD/voicemail behavior, live CDR billing and real callback call.
- [ ] Live Soniox/Parakeet attribution/finality/latency and Groq/OpenAI model availability/quotas/quality trials.
- [ ] Permissioned real-call review set, outcome trial, calibrated scorecards and proven cohort lift.
- [ ] Seller registry access and counsel-approved destination/recording/prerecorded policies.
- [ ] Mac/Windows hardware audio/NAT/Bluetooth/sleep/long-call certification and production VM/ARM64 capacity; Linux is outside preview distribution.
- [ ] Public distribution signing/notarization and configured public server/DNS/provider credentials.

User explicitly instructed continuing without unavailable APIs. External gates stay visible and associated features fail closed where required.
