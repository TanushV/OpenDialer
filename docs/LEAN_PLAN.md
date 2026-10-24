# OpenDialer lean plan (active)

Approved by the user on October 22, 2026. This is the active plan. It supersedes the mechanisms (not the features) of
[REMEDIATION_PLAN.md](REMEDIATION_PLAN.md) and [EXECUTION_SPEC.md](EXECUTION_SPEC.md), which stay as history and as the
detailed description of what each feature does. Progress and evidence go in the "Status" section at the end of this
file; [STATUS.md](STATUS.md) keeps the historical log.

## Principle

OpenDialer is for internal use. It must be lightweight, clean and fast, with no bloat, and **every end feature a rep,
manager or admin uses stays and keeps working** in local (SQLite) and hosted (PostgreSQL) modes. Code that is not a
feature goes: security hardening, legacy upgrade code, dead code, duplicate mechanisms, and acceptance or packaging
harnesses. Where a mechanism is over-built, rewrite it smaller with the same results. The AST must go down
significantly; a step that grows it is wrong.

Out of scope for now: installing or updating the Mac/Windows app. Never touch the installed app
(`/Applications/OpenDialer.app`) or its data. Verify through the backend and the browser UI.

## Features that stay

Every item below must work at the end, in both modes where it applies.

1. Workspaces: local, hosted (owner, admin, rep roles; users), simulation.
2. Leads: CSV import, list and search, endpoint correction, outreach hold, conversions (record, retract).
3. Calling: ready/pause with idle auto-pause and heartbeats; next lead (due callbacks first, coverage, statistical
   ranking, number selection); Telnyx agent and prospect legs bridged on answer; hangup; mute; keypad/DTMF; opt-out by
   button or DTMF 9; final-attempt voicemail, monitored and detached; inbound callbacks (owner, teammate, business
   fallback, spoken message, callback task); stuck-call recovery (reconcile, stop, cancel unsent, confirm not placed,
   retry a failed job).
4. Compliance: internal opt-out list first; national DNC from imported snapshots and prepaid lookups (manual and the
   scheduled refresh, off by default); state registries where policy requires them; consent evidence; RND results
   import; safety holds and their release; calling windows; operating policy; preflight.
5. Live helper: proactive tips following stable transcript chunks; private help with streaming; mute, pin, cancel;
   feedback; selective fact review; five-minute raw speech and quote lifetime; Cerebras-only route.
6. Post-call: rundown and finalization; AI scorecard, rubrics editor, call purpose, human scores and appeals;
   trackers; snippets; comments; notes; dated follow-up tasks (confirm, reschedule, snooze, assign, complete, cancel);
   rep-reported meetings and outreach holds; next-call brief and packet refresh.
7. Coaching: practice, example library, goals, coaching history, replay evaluator.
8. Preparation and research: official-site research, seller templates with gatekeeper and iPhone screening sections,
   advance and off-hours batches, prepared runway, OpenAI / compatible endpoint / ChatGPT-plan routes, metered search,
   product knowledge and playbooks, treatment variants.
9. Statistical ML and optimization: pooled estimates and selection profile; challenger training with backtests and
   calibration; reviewed activation, model trials, rollback; drift; controlled experiments, comparisons, cohorts and
   holdouts, coaching strategies; conversation learning (observations, vocabulary, associations, observation review);
   goal change preview and reconciliation; optimization report.
10. Numbers: inventory sync, configure, add, pause/resume, observed health (including case-mix), external reputation
    import, reassignment, number review, search and quotes (purchase stays blocked by policy), capacity planner
    (existing-only and minimum-addition proposals, exact small search, greedy for large, lower bound, retry scenarios),
    staffing (simple and named availability).
11. Metrics and work time: home metrics, report views, rep work intervals, corrections, rep-hour metric.
12. Costs: usage ledger, per-call estimates, budgets, carrier cost import and credits, operator reconciliation, cost
    register.
13. Admin: settings and provider keys, callback routing, users, backup and restore, webhook reachability check, local
    webhook tunnel, offline viewing in the desktop when the server is down.

## What goes

- Hardening: encryption at rest (`EncryptedJSON`, Fernet, cipher context); HMAC lookup keys (plain normalized keys
  instead); the encrypted, hash-chained journals (consent, suppression, effect, authorization) and their startup
  replay; the encrypted Vault config store (plain JSON instead); master-key handling and the keychain session store;
  native protected key dialogs (keys are entered in Settings); research-fetch DNS/SNI pinning and IP denylist (keep
  timeouts, byte and redirect caps); login-throttle extras; `secure_delete` and other hardening pragmas.
- Legacy upgrade code: installed-store staging copies, sanitation cutovers, completion contracts, raw-retention
  cutover markers, legacy readers and compatibility shims, old-schema adoption, `encrypt_legacy_records`, backfills,
  `close_retired_batches`, `close_legacy_prospect_legs`, `migrate_followthrough`, `migrate_conversation_states`,
  `migrate_registry_chunks`, the 19-step Alembic chain (one schema, created at startup), the legacy desktop raw-cache
  purge. Backup and restore stay, as plain data export and merge without journal reconciliation.
- Dead code: never-read tables (scorecard_items, transcript_revisions, batch_requests, registry_subscriptions,
  lead_phone_links, allocation_decisions, scheduling_plans, memberships, tenants if unused), never-produced call
  states, recording branches (the route always returns 410), number-order reconciliation (purchases are blocked, so no
  order can exist), retired job kinds, no-op branches, unused settings.
- Duplicate or over-built mechanisms, rewritten smaller with the same results: the incremental metric engine
  (projections, report revisions, rebuild namespaces) becomes direct queries over real columns; the per-operation
  recovery matrix becomes one reconcile-or-stop path; the inbound attempt ledger and watchdog become one routing chain;
  preparation's own lease/heartbeat/backoff uses the job queue; job-queue generation counters and claim waves; the
  CPU-priority/GIL layer and writer-admission aging if no longer needed for speed; about 15 hand-rolled data loaders
  in the UI become one hook; Rust HTTP calls and native session/cache plumbing move to the UI.
- Acceptance and packaging harnesses: `measure_completion.py` (replaced by a short `scripts/bench.py`),
  `windows_native_smoke.py`, `frozen_acceptance.py`, Windows acceptance and staging workflows, the coverage gate,
  `completion_acceptance.py`, `inspect_architecture.py` (replaced by a tiny size counter), dependency-notice
  generation. The release build path (release workflow, sidecar build, Mac packaging, manifest) stays so the app can
  ship later.
- Tests that only cover the removed machinery. Every feature keeps tests.

## Steps

Each step keeps the acceptance suite green on SQLite and PostgreSQL (and the browser suite where the UI changes),
records size, and is committed. Old tests that cover only removed machinery are deleted in the same commit.

1. **Plan** (this file), AGENTS.md pointer, and a dated scope note in the two long plans.
2. **Acceptance suite first**, at the HTTP level so it survives internal rewrites: `services/backend/tests/acceptance/`
   with journeys for every feature above, run on SQLite and PostgreSQL, plus browser specs in local and hosted modes.
   It must pass on today's code before anything else changes. Record baseline speed (`scripts/bench.py`) and size.
3. **Storage**: plain JSON documents (JSONB on PostgreSQL), real indexed columns on the busy tables with call outcome
   and timing fields promoted, plain normalized keys, no journals, plain settings store, one schema, no legacy code,
   dead tables dropped, simple backup/restore.
4. **Mechanisms**: direct-query reports and estimates; one recovery path; one inbound routing chain; preparation on the
   job lease; simpler job queue and runtime; one HTTP client; settings constants; plain session tokens.
5. **Dead code and routes** removed; startup does no history scans.
6. **Frontend and Rust**: shared data hook, offline viewing without encryption, plain key entry, deduplicated types;
   Rust keeps the sidecar launch and the webhook tunnel and drops keychain, encrypted cache, protected dialogs, Rust
   HTTP and their tests.
7. **Tests, CI, scripts, docs**: delete machinery-only tests and harnesses; CI is lint, backend SQLite, backend
   PostgreSQL, frontend unit, browser, plus the release build; rewrite README, runbooks and VALIDATION for the lean
   product.
8. **Verify end to end**: full suites in both modes, hosted compose smoke, `scripts/bench.py` locally and on the 2-vCPU
   reference, size comparison against the baseline below, and a final report.

## Targets

| Area | Baseline (a950675) | Target |
|---|---|---|
| Backend app | 39,459 lines, 270,889 Python AST nodes, 1,333 functions | about -30%: <= 28k lines, <= 190k nodes |
| Frontend src | 16,487 lines, about 117k tokens | about -20% |
| Rust shell | 2,459 lines | about -50% |
| Backend + repo tests | about 55k lines | about -65% |
| Browser and unit tests | about 9.6k lines | about -50% |
| Scripts / workflows | 5,150 / 680 lines | about -90% / -80% |

Speed (local SQLite, 10k leads / 100k attempts): next lead p95 <= 100 ms and <= 15 SQL statements; webhook receipt
<= 30 ms; event to state <= 100 ms; wrap-up <= 100 ms; workspace view <= 150 ms; startup <= 2 s with no history scans.
Hosted (2-vCPU reference, 8 reps, one call per rep per minute): the remediation section 18.2 targets (next lead p95
< 250 ms, event to state < 250 ms, wrap-up < 250 ms), measured and reported as measured.

## Decisions

- No upgrade path: fresh stores only; existing 0.2/0.3 stores are not migrated. The installed app keeps its own data
  untouched. Provider keys are stored unencrypted on the authority (internal use).
- Storage keeps the document shape (JSON `data` per row) with real columns on busy tables; no rewrite to fully typed
  tables.
- The release build path stays; native acceptance and staging machinery go.
- This file is the active plan; AGENTS.md points to it.

## Working rules

- Tests before changes; a newly found defect gets a regression test first.
- Commit coherent steps often; no AI attribution anywhere (commits, PRs, releases).
- Never commit credentials, databases, recordings, build products or caches.
- Disposable PostgreSQL for tests: `postgresql+psycopg://opendialer:remediation-disposable-test@127.0.0.1:55439/opendialer_remediation_test`
  (browser: database `opendialer_completion_e2e`), container `opendialer-remediation-postgres`.
- Simulated providers are evidence about software only; live carrier, funded inference, DNC vendor, hardware and
  legal gates stay external (X01-X08).

## Status

- Step 1 done (98a9018).
- Step 2 done (c4e9707). `services/backend/tests/acceptance/`: 99 HTTP journeys (98 pass, 1 strict xfail for a
  found defect: task.updated events carry no rep scope, so in hosted mode another rep sees a callback task's id and
  time). Green on SQLite (1m52s, -n 6) and PostgreSQL (4m54s, -n 6). One seam touches internals:
  `test_feature_models.install_fake_models` replaces `service.configured_coach` and turns simulation off for model
  output; keep that seam stable. Not drivable in simulation (covered by provider contract tests only): prepaid DNC
  success, live ingress success, simulated agent-leg uncertainty. Browser baseline: 156/156 on a quiet machine
  (3 load flakes when run alongside other suites, all passing on rerun).
- Baseline speed (`scripts/bench.py`, 10k leads, 100k attempts, 40 samples, quiet Mac, a950675): next lead p50 38.6 /
  p95 82.1 ms with 28 SQL statements; webhook receipt 7.3 / 13.2 ms; event to state 15.6 / 33.6 ms; wrap-up
  19.6 / 32.7 ms; workspace 40.0 / 73.5 ms; reopen 144 ms; store 374 MB.
- Baseline size (`scripts/size.py`): backend 88 files / 39,459 lines / 270,889 nodes; backend tests 52,034 lines;
  repo tests 3,027; frontend 16,487 lines / 89,588 tokens; frontend tests 9,569; Rust 2,463; scripts 5,211;
  workflows 680.
- Step 3 done (588a2b4, 85f0670): plain JSON documents and plain lookup keys; no journals, Vault encryption,
  startup contract, Alembic chain or legacy cutovers; plain JSON config store; opaque session tokens; plain backups
  merged on restore; nine dead tables dropped; research-fetch pinning and login-throttle extras removed. Old tests
  that covered only removed machinery deleted (backend tests 52k -> 9.8k lines); kept unit tests share `helpers.py`.
- Step 4 done (f0365dd, 6b5416f, b9bc30a, f5b60d3, a9a129c): reports computed directly from source records with the same
  per-record definitions (no contribution/bucket tables, dirty units, rebuild namespaces or cleanup jobs); the recovery
  policy table replaced by one stop set; simpler job runner, priority gate and SQLite writer lock; next lead ranks and
  claims in one transaction (28 -> 15 SQL statements); preparation heartbeats dropped. Runtime limits are plain
  constants and the three HTTP pools are one shared client (a26f5a3). Inbound return calls already run as one routing
  chain on the task (attempts inline, one watchdog); preparation keeps a plain expiring per-lead marker so two
  batches cannot pay for the same lead.
- Step 5 done: number-order reconciliation, refused purchase and recording stubs, retired model formats, legacy shims,
  redundant admin re-checks and host/origin middleware removed. The acceptance suite's pinned defect (task events
  visible to other reps) is fixed, and it found a real PostgreSQL race (a hangup applied concurrently with its leg's
  answer stalled callback fall-through); webhooks now apply in arrival order.
- Step 6 done (37275ac): Rust shell 2,463 -> 965 lines (sidecar launch and webhook tunnel only); UI keeps sessions,
  opt-out queue and offline copies in local storage with raw speech and helper text stripped, enters provider keys
  inline, and shares one loader hook; number orders UI removed.
- Step 7 done: CI is lint, backend SQLite + PostgreSQL, frontend, browser and desktop shell (runs in ~7 minutes);
  release workflow keeps the signed build; harness scripts and Windows acceptance/staging workflows deleted;
  README, runbooks, security policy and architecture notes rewritten for the lean build.
- Step 8 (October 23, 2026):
  - Suites: backend + repo tests 579 passed on SQLite and 580 passed on PostgreSQL (each with its own skips);
    frontend typecheck, format, build and 74 unit tests pass; Rust fmt, tests and clippy pass; GitHub CI green.
  - Hosted compose smoke (`docs/evidence/lean/hosted-smoke.json`): migrate job, API, background worker and
    PostgreSQL complete bootstrap, import, worker preparation, a rep call through wrap-up with a callback task, and
    an API/worker restart that keeps both; no error lines.
  - Local speed, 10k leads / 100k attempts, baseline (c4e9707) and lean run back to back in both orders on the same
    shared Mac (load average 15-30). Lean first, p50 / p95 ms: next lead 101 / 176 -> 25 / 80; webhook receipt
    15 / 29 -> 5 / 13; event to state 36 / 68 -> 10 / 22; wrap-up 49 / 69 -> 14 / 33; workspace 94 / 204 -> 29 / 71;
    startup 359 -> 74 ms; store 374 -> 308 MB. Final code under two hash seeds: next lead 25-27 / 50-55 ms with 15
    SQL statements, webhook 6-7 / 13, event 11-12 / 26, wrap-up 16-18 / 24-31, workspace 35-40 / 68-77, startup
    74-94 ms; every local target met.
  - Found while benching: some runs read the workspace in about 1 s. The fixed SQLite statistics rated the partial
    open-calls index the same as the full owner index, so the plan followed index creation order, which follows
    the process's string hash seed; a rep owning 100k finalized calls then scanned all of them. Partial indexes now
    estimate fewer rows (`test_sqlite_plans.py` checks both creation orders); the same store reads in 28 ms instead
    of 580 ms.
  - Hosted, 8 reps each starting one call per minute for 5 minutes on the 2-vCPU reference shape (1.5 vCPU app +
    0.5 vCPU PostgreSQL sharing a network namespace), 40 journeys and no failures in each run. With every lane in the
    API process: next lead p50 100 / p95 790 ms, event to state 34 / 85, wrap-up 49 / 96. `bench.py --hosted` now
    lays processes out like compose (calling lanes in the API, research/scripts/ML in a second process); that run,
    on a busier host: next lead 198 / 1217, event to state 58 / 209, wrap-up 80 / 270. Next lead misses the 250 ms
    p95 target in both, and Docker CPU caps on this oversubscribed Mac cap but do not reserve CPU, so the hosted
    numbers stay a gate for a dedicated host. The baseline on the same shape had selection p95 24.8 s with 27 of
    224 journeys timing out.
  - Size versus a950675: backend 39,459 -> 33,972 lines, 270,889 -> 233,433 Python AST nodes (-14%, short of the
    -30% target); backend + repo tests 55,061 -> 10,435 lines (-81%); Rust 2,463 -> 965 (-61%); scripts 5,211 ->
    1,388 (-73%); workflows 680 -> 186 (-73%); frontend 16,487 -> 16,171 lines (-2%, short of -20%); browser and
    unit tests 9,569 -> 9,344 (-2%). All Python together: 687k -> 313k AST nodes (-54%). What remains in the backend
    is the kept features' own logic (capacity planner, statistical ML and learning, coaching state, compliance,
    telephony recovery); a scan for repeated code finds only four small duplicates. Reaching -30% would mean
    rewriting those algorithms rather than removing bloat, so the target is reported as missed.
- Plan complete (October 23, 2026). Every kept feature passes its HTTP journeys on SQLite and PostgreSQL and the
  browser journeys in both modes.
- Ready for live testing (October 23, 2026), version 0.4.0:
  - Packaging: both sidecars freeze; the frozen authority runs 200 simulated calls with metrics and post-call
    analysis; the frozen trainer trains the challenger in the acceptance training journey; the 0.4.0 bundle seals,
    verifies, starts both sidecars under the hardened runtime and passes archive verification, stopping only at the
    updater signature, whose key lives in the release workflow's secrets.
  - Old installs: a 0.3.3 store used to open and then fail at login (HTTP 500). The desktop now keeps 0.4.0 data in
    `local-2/` and `simulation-2/`, leaving 0.2/0.3 folders untouched, and the backend refuses a 0.2/0.3 database
    with a plain message (`test_store_format.py`; checked on a real 0.3.3 store, left byte-identical).
  - Open items: the backend (-14%) and frontend (-2%) size targets were missed. Live testing remains: Telnyx calls
    and audio, funded Cerebras, DNC vendor access, provider retention terms, hosted latency on a dedicated host and
    campaign lift. Publishing 0.4.0 to existing installs starts them on a fresh workspace.
  - Draft release: the signed release workflow built v0.4.0 from a05a196 (run 40066009752, replacing the
    earlier draft from 4565d15, which predated the strategy-policy fix): Mac archive and DMG,
    Windows NSIS and MSI, updater signatures, `latest.json` (version 0.4.0) and checksums. Not published; v0.3.3
    stays the latest release, so installed updaters do not see it.
- Coverage restored (October 23, 2026): a coverage run found kept features whose tests the lean cleanup had deleted.
  New tests now cover the scheduled prepaid DNC refresh (budget stop, opt-outs never queried, one job per window),
  conversation associations (restored unit tests), coaching strategy choice (reviewed example, logged 50/50
  assignments, matured rewards), official-site research (domain and www only, two ranked pages, quote-checked
  facts, unreadable sources fail), helper usage metrics in the optimization report, and the desktop launch handshake
  (stdin token, launcher loss, one authority per folder). They caught one defect: the strategy policy named an
  approach "best" as soon as it reached ten outcomes, even with no conversions, which in adaptive studies would have
  sent 95% of leads to it; the reviewed default now keeps the lead unless a supported approach estimates better.
  Unread helper usage counts were removed. Backend line coverage 81% -> 84%.
- Verification completed (October 24, 2026): [receipt](evidence/lean/verification-20261024.json), current
  [handoff](HANDOFF.md). All 131 mapped routes match source. Added 21 provider-path cases; four failing
  regressions preceded the fix for ChatGPT-plan streaming accepting non-object JSON (`538dcde`).
  - SQLite: 597 full-suite + 3 added number-provider cases pass (600 distinct); PostgreSQL: 598 + 3 (601).
    Backend line coverage 84.8%. Caddy route/method tests separately enabled: 2 pass.
  - Browser: initial run 153 pass / 1 failure because a meeting review encountered a second valid context
    conflict. Two injected conflicts reproduced the test assumption. `f9eacca` verifies preserved drafts and
    explicit review/retry without weakening the app's 409 checks. Fresh full rerun: 154/154 pass in 6.3 minutes.
  - Frontend typecheck/format/build/74 unit tests and Rust fmt/7 tests/clippy pass.
  - Both frozen sidecars rebuilt: authority call/callback and restart pass; trainer fits a human stage from
    600 synthetic rows and exports a checksum-valid model. Compose 0.4.0 bootstrap/import/background preparation,
    call/callback and API/worker restart pass with no runtime error lines. A synthetic 0.3 marker is refused with
    exit 2 and an unchanged logical PostgreSQL dump. Task-owned compose containers and volumes were removed.
  - Local speed, 10k leads / 100k attempts / 40 samples per hash seed: next-lead p95 47.8-53.5 ms with 15 SQL
    statements, webhook 8.3 ms, event-to-state 11.9-14.3 ms, wrap-up 15.2-22.5 ms, workspace 34.0-57.1 ms,
    startup 61-78 ms. All local targets pass under both seeds.
  - Hosted workload on an uncapped shared Mac (native API + separate background process, Docker PostgreSQL),
    2,000 leads, 8 reps x one call/min for five minutes: 40 journeys, zero failures; p95 next lead 141.1 ms,
    event-to-state 224.3 ms, wrap-up 198.2 ms. These measurements do not certify the dedicated 2-vCPU reference
    or replace the earlier capped-host miss; dedicated hosting remains an external gate.
  - Installed app/data untouched; no paid provider actions, publication or release build. New commits are local,
    with no new GitHub CI run. The existing signed draft predates the inference fix. Live accounts, hardware,
    clean-machine Mac/Windows installers, provider terms/legal approval and campaign lift remain unverified.
    Backend/frontend size targets remain missed; coverage gaps remain listed in the current handoff.
