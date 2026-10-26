# OpenDialer testing handoff (CURRENT)

> **Status: CURRENT — unreleased setup/journey fixes locally verified on 0.5.1**, October 26, 2026.
> Historical 0.5.0 release: `096c55f`, CI 40164184233 passed all jobs including 164 browser cases.
> Includes script learning and the review fixes described below. This is the single current handoff.
> Read with [HANDOFF_AST.md](HANDOFF_AST.md) and [SCRIPT_LEARNING.md](SCRIPT_LEARNING.md).

OpenDialer 0.5.0 simplifies calling, scripts and learning. The current local verification is recorded below; older results are explicitly historical.
Live-provider and clean-machine installer gates remain open. Continue finding what the tests do not prove,
adding tests before fixes, and supporting approved live testing. Read this file, then `AGENTS.md`, then the
Status section of `docs/LEAN_PLAN.md`.

## October 26 setup and journey audit (locally verified, unreleased)

The owner's new request replaces the blanket new-rental ban with exact-quote approval.
Numbers now supports maximum owned count, maximum initial cost per number and a monthly cap on app-ordered rentals. Zero limits block purchases. Search is read-only; approval rechecks the price and creates one durable intent. Repeating or reconciling an uncertain order never resubmits it. Routing, identity and real callback checks still gate outbound use. Existing carrier rentals, taxes and usage remain separate from the app-order rental cap.

Setup puts ChatGPT beside OpenRouter, saves the Telnyx key and connection in one action, allows clearing optional fields, refreshes provider versions across tabs, and provides retries for script and inventory reads. SSH ingress lives beside Telnyx. The installed app and real provider accounts are untouched during this audit. Current evidence: [receipt](evidence/lean/setup-journeys-20261026.json), runtime source `3ebd4c7`. Full suites: SQLite 677 / 2 skips, PostgreSQL 680 / 1; later targeted additions/corrections bring distinct passing cases to 682 / 683. All 171 browser journeys have passing evidence (161 full-run passes, then focused reruns); 77 frontend and seven Rust tests, formatting/typecheck/build/Clippy pass. The receipt preserves failed runs and corrections. Logs and visual checks are under ignored `work/setup-audit/`. The 0.5.1 results below are historical for this new source.

Telnyx number readiness now reads recording evidence from the documented voice endpoint. Number ordering also preserves drafts across version conflicts and stale reads, and reloads its durable order status after a lost HTTP response. Live provisioning, audio, funded inference, account eligibility and clean-machine installation remain unverified.

## Previous 0.5.1 changes

- Prerecorded voicemail drop controls and endpoint removed. Previously queued outbound playback/gather commands cannot send a message. In-flight cleanup remains available.
- New campaigns default to 08:00–21:00 prospect-local time. Existing published hours are preserved until an explicit Settings edit. The owner requested updating this Mac’s campaign hours and signed app installation.
- Regression tests cover opening/closing boundaries in Eastern/Pacific time in winter and summer, removed drop routes, and stale queued drops.

## Previous 0.5.1 verification

[Receipt](evidence/lean/voicemail-hours-051-20261025.json), runtime source `0976008`.
SQLite 670 passed / 2 skips; PostgreSQL 671 / 1; frontend 75 tests, format/typecheck/build; Rust 7 tests, fmt and Clippy passed.
Browser: 163 passed in the full run, followed by both rubric tests passing after correcting a false-positive save assertion and handling explicit version conflicts. All 164 cases have passing local evidence; no application timeout or concurrency guard was weakened.
Frozen backend/trainer, Mac bundle/archive/DMG, extracted simulated call/callback and restart, and trainer self-check passed. Local updater signing used a disposable key, deleted afterward. Optional Caddy tests remain skipped. Temporary native PostgreSQL is stopped.
The existing Mac workspace’s hours were explicitly published as 08:00–21:00 through Settings. Production-signed downloads, exact-SHA CI and installed-app verification follow through GitHub Actions and the signed updater; live provider gates remain open.

## Retained 0.5.0 changes

- Calling: Space / Next call requests the scheduler's next eligible lead; second attempts remain scheduler-selected. Optional automatic advance after ended voicemail has a five-second cancel window and respects Pause after call. Reloading restores only the current rep's unexpired reservation and loads its script before enabling the call.
- Scripts: upload Markdown/text in Intelligence, preview headings as sections, then adopt with version checks. Plain script wording is displayed during calls; learned variations retain version history.
- Intelligence: script first, expandable memory/variations, statistical learning and product facts. Practice prioritizes reviewed improvement patterns and supported script findings.
- Post-call: known carrier outcomes and supported independently reviewed AI outcomes save automatically; humans can correct them. Ambiguous outcomes, bookings and callback times require review. AI labels do not become observed training labels without human confirmation.
- Actions: research and post-call models submit named function calls. Application validation executes updates; free-form text cannot trigger those actions. Live helper presentation remains streamed.
- Numbers: effective workload adapts to delivery health within the reviewed ceiling. Existing allocation and staffing planning remain authoritative; no automatic purchase was added. Planning saves are serialized and preserve edits made during a pending save.
- Release builds hide simulation entry and controls. The development/test mode remains available for software checks.

## Previous 0.5.0 verification

[Verification receipt](evidence/lean/calling-ux-050-20261024.json). Logs and failure traces are retained in ignored `work/ux-0.5.0/`.

| Check | Result |
|---|---|
| Backend and repo tests | Fresh full suites: SQLite 652 passed / 2 skipped; PostgreSQL 653 / 1. Native disposable PostgreSQL 18.3; CI uses 17. |
| Browser | 164 cases covered: 161 passed in the final full run, then all three failed cases passed in the targeted rerun. One was a connected brief timeout under load; two now await import completion and explicitly submit history search. No timeout increases. |
| Frontend and Rust | Typecheck, formatting, production build, 75 frontend tests; Rust fmt, 7 tests and clippy pass. Production assets omit simulation entry and call controls. |
| Mac package | Both sidecars rebuilt; bundle and extracted archive seals, DMG checksum, hardened-runtime sidecar checks pass. Extracted authority imports 20, prepares 6, completes a simulated callback and preserves it through restart; trainer self-check passes. Disposable updater key only, deleted afterward. |
| Regression fixes | Pending planning edits survive an earlier save; a reload restores only the current rep's unexpired reservation; hidden correction forms do not duplicate Review controls; simulated workspaces cannot select a paid coach. Mailbox and answered-call outcome boundaries are covered. |
| CI and downloads | [CI](https://github.com/TanushV/OpenDialer/actions/workflows/ci.yml), [signed release workflow](https://github.com/TanushV/OpenDialer/actions/workflows/release.yml), [releases](https://github.com/TanushV/OpenDialer/releases). Check the exact SHA and release assets there. |
| Remaining gates | Fresh 0.5 hosted Compose smoke blocked by unavailable Docker. Native GUI/clean-machine install, real accounts/carrier/audio, dedicated hosting and outcome lift remain unverified. Installed app/data were not changed under the latest working agreement. |

The temporary native test PostgreSQL instance is stopped. The original disposable Docker container and its data remain intact but stopped: restoration failed because Docker was unavailable. Start `opendialer-remediation-postgres` when that service is healthy; do not reset unrelated workloads.

## 1. What OpenDialer is

A desktop calling workspace for small internal sales teams. One FastAPI authority (`services/backend/app`) serves a
React/TypeScript UI (`apps/desktop/src`) inside a small Tauri shell (`apps/desktop/src-tauri`). Two modes, same code:

- **Local**: the desktop launches a bundled authority on a random loopback port with SQLite in its data folder.
- **Hosted (connected)**: a shared server on PostgreSQL (`deploy/compose.yml`: migrate job, API, background worker,
  PostgreSQL, Caddy). Desktops sign in to it.

Providers: Telnyx (calls, WebRTC agent leg, numbers, signed webhooks), Cerebras through OpenRouter (live helper and
rundowns), OpenAI / OpenAI-compatible endpoint / a rep's ChatGPT plan (research and scripts), donotcalldnc.com
(prepaid DNC lookups). **Simulation mode** replaces all of them with simulated providers and a fixed clock.

## 2. Verification history before 0.5.0

| Item | State |
|---|---|
| Version | 0.4.0 everywhere the distribution contract checks (`tests/test_distribution_contract.py`) |
| Verified source | `74658b4` fixes script-test control drift, stale-winner promotion and prepared-script memory caching. Fresh local CI and Mac verification: [receipt](evidence/lean/script-learning-fixes-20261024.json). Remote results are tracked in [Actions](https://github.com/TanushV/OpenDialer/actions/workflows/ci.yml). |
| Backend + repo tests | Fresh full suites: 628 passed / 2 skipped on SQLite; 629 passed / 1 skipped on PostgreSQL. The optional Caddy image test is skipped in this run; the guarded-PostgreSQL check is inapplicable to SQLite. |
| HTTP acceptance journeys | 113 test functions in `services/backend/tests/acceptance/`; parameterized cases run in both database suites. |
| Browser journeys | Fresh full rerun: 155/155 pass in local/connected modes. An earlier run under concurrent backend/build load was stopped after two interaction failures; traces retained. No assertion or timeout changes. |
| Frontend unit / Rust | 74 Vitest tests; Rust fmt, 7 tests, clippy pass |
| Backend line coverage | 84.8% (13,380 / 15,778 statements); this is not full branch coverage. Gaps remain in section 9. |
| Draft release (October 23 snapshot) | **v0.4.0, signed, UNPUBLISHED**, built from `a05a196` by [run 40066009752](https://github.com/TanushV/OpenDialer/actions/runs/40066009752): Mac archive + DMG (aarch64), Windows NSIS + MSI (x64), updater signatures, `latest.json`, `SHA256SUMS.txt`. The draft predates the `538dcde` inference fix; it was not rebuilt or published during verification. v0.3.3 was latest published at the previous handoff; remote release state was not rechecked. |
| Packaging | Both sidecars rebuilt on October 24. Direct frozen-authority smoke passed import, preparation, call, callback and restart persistence. Frozen trainer fitted the human stage from 600 synthetic rows and exported a checksum-valid model. The current Mac app now builds, seals, verifies after archive extraction, and passes simulated call/callback/restart and trainer checks. DMG creation/checksum pass. Updater signing used a disposable test key (deleted afterward); these artifacts are not production updates. Clean-machine installation, native GUI, notarization and production-key signing remain unverified. |
| Local speed | Fresh 10k leads / 100k attempts, 40 samples under each of two hash seeds: next lead p95 47.8-53.5 ms with 15 SQL statements, wrap-up 15.2-22.5 ms, workspace 34.0-57.1 ms, startup 61-78 ms. All local targets met. |
| Hosted speed | October 24 uncapped shared Mac, separate API/background processes, 8 reps x 1 call/min for 5 minutes: 40 journeys, zero failures; p95 next lead 141.1 ms, event-to-state 224.3 ms, wrap-up 198.2 ms. All observed p95 values are below 250 ms. This does not certify the dedicated 2-vCPU reference: earlier capped shared-host runs missed it at 0.8-1.2 s next-lead p95. |
| Hosted compose smoke | Rebuilt 0.4.0 source passes bootstrap, 20-lead import, separate-worker preparation, call/callback, API/worker restart persistence and zero runtime error lines. A synthetic 0.3 marker is refused with exit 2 and an unchanged logical database dump. See the October 24 evidence. |

### Earlier October 24 verification details

Receipt: [verification-20261024.json](evidence/lean/verification-20261024.json). Full local logs and the original
browser failure trace are in ignored `work/verification-20261024/`.

- **Product defect fixed (`538dcde`):** streamed ChatGPT-plan results accepted arrays, null, strings and numbers
  despite callers requiring an object. Four regression cases failed before the fix and now pass.
- **21 added cases:** malformed streamed output, owned-number success/rejection/lost-response reconciliation,
  paginated inventory failure, plan-token refresh and revoked scope, and research redirect/content/size limits.
  Provider HTTP is mocked; these are software contracts, not recorded live-provider receipts.
- **Browser test corrected (`f9eacca`):** the initial full run had 153 passes and one failure after a second valid
  review conflict. The trace showed `preparation_revision` changing, while the app retained the draft and returned
  409. Two injected conflicts reproduced the test failure. The test now explicitly reviews and retries each conflict,
  verifies preserved choices and notes, and requires a successful save. App concurrency checks were not weakened.
  Both focused modes and the fresh full 154-test run pass.
- Frontend typecheck, formatting, production build and 74 unit tests pass; Rust fmt, 7 tests and clippy pass.
  Vitest alone covers 13.66% of frontend lines; browser coverage is not included in that number.
- The disposable Caddy route/method test passes. Task-owned compose containers and volumes were removed.
  Installed app/data, live accounts and release publication were untouched.
- The AST map was regenerated after script learning: 139 route declarations (9 script routes added, `/v1/admin/variants`
  removed).
- Script learning (October 24, after the verification above) learns how prospects react to each section of a
  campaign's base script, keeps a Markdown memory per campaign, and tests changes before adopting them. The 0.4.0
  draft release predates it. Its tests: `test_feature_script_learning.py` (12 journeys / 13 cases), `test_script_learning.py`
  (15), `script-memory.spec.ts`.

### Script-learning review fixes (October 24)

Source `74658b4`; [verification receipt](evidence/lean/script-learning-fixes-20261024.json); local logs in ignored `work/update-review-20261024/`.
Four regression cases failed before the fixes and pass afterward on both databases:

- Both script-test arms render the templates saved at test start. Editing the seller template cannot silently
  change the control arm. A changed base withdraws the old test or prevents manual approval of its old winner.
- The bounded script-writing memory feeds the existing preparation source key. Changed notes or supported
  findings invalidate prepared scripts and supersede in-flight results. A timestamp-only refresh keeps the cache.
- Fresh full backend suites, frontend checks/74 unit tests, Rust fmt/7 tests/clippy, both sidecar builds and
  155 browser cases pass. The added cache-stability assertion also passes separately on SQLite and PostgreSQL.
- The current Mac build passes bundle/archive verification, hardened-runtime sidecar checks, DMG checksum,
  extracted-authority simulation and restart persistence, and extracted-trainer imports. Updater signing used
  a disposable test key that was deleted. Installed app/data and production updater configuration are untouched.
- Benchmarks, coverage and compose receipts elsewhere in this handoff predate these script-learning fixes;
  they were not rerun for this correction. Live-provider, dedicated-host and clean-machine gates remain open.

## 3. Rules you must follow

These come from the owner and `AGENTS.md`. Breaking them causes real damage.

1. **Never touch the installed app or its data.** Do not run, update or modify `/Applications/OpenDialer.app`, and
   do not open its data folder (`~/Library/Application Support/com.opendialer.desktop/` on macOS,
   `%APPDATA%\com.opendialer.desktop\` on Windows). **Do not launch a locally built `OpenDialer.app`**: it uses the
   same data folder. Test the frozen sidecars directly with a temporary `--data-dir` instead (section 5.7).
2. **Do not publish the draft release**, and do not run the release workflow again unless the owner asks.
3. **Disposable databases only.** `scripts/disposable_database.py` admits exactly the URLs in section 4.2; never
   point tests, benches or servers at any other database.
4. **Never commit** credentials, `.env` files, app databases, recordings, build products (`work/`, `dist/`,
   `target/`, `apps/desktop/src-tauri/binaries/`) or test caches.
5. **No AI attribution** in commits, PRs or releases: no `Co-Authored-By`, no "generated with" lines.
6. **Tests before fixes**: add a failing regression test, then fix (AGENTS.md). Keep suites green on SQLite and
   PostgreSQL.
7. **Simulated providers prove software behavior only.** Never report a live integration as working because a
   simulated or mocked test passed. Live gates stay explicit (section 10).
8. **No paid or outward actions without the owner's explicit OK**: live calls, provider API calls with real keys,
   DNC lookups, inference spend, messages, publishing.
9. Fresh stores only: 0.4/0.5 do not read 0.2/0.3 data (by decision). The desktop keeps their data in `local-2/` and
   `simulation-2/`; a server refuses a 0.2/0.3 database with a plain message.

## 4. Setup

### 4.1 Toolchain

Python 3.12 with `uv`, Node 24, Rust stable with Tauri prerequisites, Docker (for PostgreSQL and the compose smoke).

```sh
uv sync --frozen --python 3.12
npm --prefix apps/desktop ci
(cd apps/desktop && npx playwright install chromium)
```

### 4.2 Disposable PostgreSQL

The only admitted local identity is user `opendialer`, password `remediation-disposable-test`, host `127.0.0.1`,
port `55439`, with these database names:

| Purpose | Database | Used by |
|---|---|---|
| Backend tests | `opendialer_remediation_test` (xdist workers append `_gwN`) | `OPENDIALER_TEST_POSTGRES_URL` |
| Browser tests | `opendialer_completion_e2e` | `OPENDIALER_E2E_DATABASE_URL` |
| Hosted bench | `opendialer_completion_performance` | `scripts/bench.py --hosted` |

```sh
docker run -d --name opendialer-remediation-postgres -e POSTGRES_USER=opendialer \
  -e POSTGRES_PASSWORD=remediation-disposable-test -e POSTGRES_DB=opendialer_remediation_test \
  -p 127.0.0.1:55439:5432 postgres:17-alpine
docker exec opendialer-remediation-postgres psql -U opendialer -d opendialer_remediation_test \
  -c "CREATE DATABASE opendialer_completion_e2e" -c "CREATE DATABASE opendialer_completion_performance"
```

On the owner's Mac this container already exists; check whether it is running before starting tests. If a browser or bench run fails with "This
database was created by OpenDialer 0.2 or 0.3", the disposable database holds an old schema: drop and recreate that
one database (it is disposable).

## 5. How to run things

All commands from the repository root unless noted.

### 5.1 Lint and format

```sh
.venv/bin/ruff check services/backend tests scripts deploy/backup
.venv/bin/ruff format --check services/backend tests scripts deploy/backup
npm --prefix apps/desktop run typecheck && npm --prefix apps/desktop run format:check
```

### 5.2 Backend suites

```sh
.venv/bin/python -m pytest services/backend/tests tests -q -n 6 -p no:cacheprovider
OPENDIALER_TEST_POSTGRES_URL=postgresql+psycopg://opendialer:remediation-disposable-test@127.0.0.1:55439/opendialer_remediation_test \
  .venv/bin/python -m pytest services/backend/tests tests -q -n 6 -p no:cacheprovider
```

October 24 timings: original SQLite suite 4 minutes; expanded SQLite coverage run 8m53s; PostgreSQL
20m29s while other verification work shared the host. Run browser and performance checks separately from heavy suites.

### 5.3 Coverage (tooling is not in the lock on purpose)

```sh
uv run --frozen --with pytest-cov --with coverage python -m pytest services/backend/tests tests -q -n 6 \
  -p no:cacheprovider --cov=services/backend/app --cov-report=term-missing:skip-covered
```

Subprocess-driven tests (CLI, frozen binaries) do not show in coverage even when they pass.

### 5.4 Frontend unit and browser journeys

```sh
npm --prefix apps/desktop test
cd apps/desktop && OPENDIALER_E2E_DATABASE_URL=postgresql+psycopg://opendialer:remediation-disposable-test@127.0.0.1:55439/opendialer_completion_e2e \
  npm run test:e2e
```

Playwright starts three servers itself: `scripts/e2e_server.py local` (8765), `... connected` (8766, resets the
browser database) and the Vite dev server (1420). Workers = 1; the October 24 fresh full run took 6.3 minutes (the earlier loaded run took 12.2 minutes).

### 5.5 Rust shell

```sh
cd apps/desktop/src-tauri && cargo fmt --check && cargo test -q && cargo clippy -q --all-targets -- -D warnings
```

### 5.6 Try the app by hand (simulation, safe)

```sh
.venv/bin/python scripts/e2e_server.py local          # simulated local authority on 127.0.0.1:8765
cd apps/desktop && VITE_BACKEND_URL=http://127.0.0.1:8765 VITE_LOCAL_TOKEN=e2e-local-token npm run dev
```

Open `http://127.0.0.1:1420`, choose **Try a simulated workspace**, then: Go ready → Get next lead → Start call →
simulate answer → add a turn → Hang up → Save outcome → Review. The in-app browser or Playwright can drive it. Data
lives in a temporary folder and disappears when the server stops.

### 5.7 Frozen sidecars (what ships), without touching the installed app

```sh
.venv/bin/python scripts/build_sidecar.py            # writes apps/desktop/src-tauri/binaries/*-<triple>
apps/desktop/src-tauri/binaries/opendialer-trainer-aarch64-apple-darwin --self-check
mkdir -p /tmp/od-frozen && OPENDIALER_LOCAL_TOKEN=$(uuidgen)$(uuidgen) \
  apps/desktop/src-tauri/binaries/opendialer-backend-aarch64-apple-darwin serve --host 127.0.0.1 --port 18990 \
  --mode local --simulation --data-dir /tmp/od-frozen
```

The local `binaries/` folder may hold a stale build; rebuild before testing. `scripts/package_macos.py` seals and
verifies a bundle but stops at the updater signature (the key lives only in GitHub secrets). A plain
`npm run tauri build` re-signs the sidecars ad hoc and the trainer then fails to load; that is expected and is why
the release uses `package_macos.py`.

### 5.8 Speed and size

```sh
.venv/bin/python scripts/bench.py --leads 10000 --attempts 100000 --samples 40      # local SQLite
.venv/bin/python scripts/bench.py --hosted postgresql+psycopg://opendialer:remediation-disposable-test@127.0.0.1:55439/opendialer_completion_performance \
  --leads 2000 --reps 8 --cadence 60 --duration 300                                # API + separate worker, like compose
.venv/bin/python scripts/size.py
```

The owner's Mac runs heavy unrelated workloads (load averages 15-400 were seen). Single runs swing up to 10x; compare
only back-to-back runs, and run each comparison in both orders.

### 5.9 Hosted compose smoke

Follow `deploy/runbooks/deploy.md` with a disposable env file (bootstrap token, PostgreSQL password,
`OPENDIALER_SIMULATION=true` for a smoke), bind the API to loopback, then bootstrap, import leads, let the worker
prepare them, place and wrap up a call, restart the API and worker, and confirm the data survives and logs have no
errors. The latest result is in `docs/evidence/lean/verification-20261024.json` (product source `538dcde`).
`hosted-smoke.json` retains the earlier `a9a129c` receipt. For simulation, use a compose override setting
`OPENDIALER_SIMULATION: "true"` on migrate, api and worker-background; an env-file value alone does not override
the literal false in compose. Bind only a disposable loopback port and remove only the task-owned project afterward.

## 6. Where things are

| Path | What |
|---|---|
| `services/backend/app/main.py` | `create_app`, settings and most HTTP routes |
| `services/backend/app/intelligence.py` | `Application` (the composed service), preparation/research, job runner, maintenance tick |
| `service.py`, `rep_workflows.py`, `workspace.py`, `measurement.py` | calls, dispositions, tasks, presence/work time, workspace snapshot |
| `scheduling/` | next-lead selection and claim (`queue.py`), ranking policy and models |
| `telephony/` | carrier events, webhooks (`ingress.py`), inbound return calls (`callbacks.py`), stuck-call recovery |
| `compliance/` | opt-outs, registries, consent, operating policy, prepaid DNC screening and scheduled refresh |
| `coaching/`, `postcall/` | live helper, conversation state, rundown, scorecards, rubrics, library, replay |
| `optimization/` | estimates, reports (`projections.py`), experiments and strategies, learning, capacity planner, numbers |
| `ml/` | training (separate trainer process), serving |
| `providers/` | Telnyx, OpenRouter, OpenAI-style inference, ChatGPT-plan OAuth, DNC, simulated carrier |
| `db.py`, `operational_schema.py`, `reporting.py`, `jobs/` | storage, indexes, report revisions, job queue |
| `services/backend/tests/acceptance/` | HTTP journeys per feature area; `journey.py` holds the helpers |
| `services/backend/tests/` | unit tests; `conftest.py` (fixtures: `app`, `client`, `workspace`, `clock`, `carrier`) and `helpers.py` |
| `tests/` | repo-level contracts (distribution/version identity, disposable DB guard, macOS packager, ingress proxy) |
| `apps/desktop/src` | UI (`App.tsx`, `context.tsx`, `api/client.ts`, `components/`, `state/`) |
| `apps/desktop/tests` | `*.spec.ts` Playwright, `*.test.ts` Vitest, `fixtures.ts` shared Playwright fixture, `simulation.ts` helpers |
| `apps/desktop/src-tauri/src/lib.rs` | desktop shell: launches the sidecar, SSH webhook tunnel, updater cleanup |
| `scripts/` | bench, size, sidecar build, macOS packager, release manifest, e2e servers, coach evaluator |
| `deploy/` | Dockerfile, compose, Caddy, backup, runbooks (deploy, providers, recovery, releases) |
| `.github/workflows/` | `ci.yml` (every push), `release.yml` (manual signed draft) |

## 7. Features and where they are tested

The kept feature list is in `docs/LEAN_PLAN.md` ("Features that stay"). Main coverage:

| Feature area | HTTP journeys (`services/backend/tests/acceptance/`) | Other tests |
|---|---|---|
| Workspaces, roles, sessions | `test_feature_workspace.py`, `test_feature_smoke.py` | `test_desktop_launch.py`, `admin-setup.spec.ts`, `completion.spec.ts` |
| Leads, import, correction, holds, conversions | `test_feature_workspace.py`, `test_feature_calling.py` | `workspace-pages.spec.ts`, `followthrough.spec.ts`, `csv.test.ts` |
| Calling, next lead, keypad, voicemail, inbound, recovery | `test_feature_calling.py` | `test_planner_correctness.py`, `test_unified_planning.py`, `call-states.spec.ts`, `keypad*.spec.ts`, `recovery.spec.ts`, `callback-routing.spec.ts`, `telephony.test.ts` |
| Compliance, DNC (manual and scheduled), consent, windows, preflight | `test_feature_compliance.py` | `test_policy.py`, `dnc-recovery.spec.ts` |
| Live helper | `test_feature_live_helper.py`, `test_feature_models.py` | `test_coach_stream.py`, `test_openrouter_coach.py`, `test_grounding_review.py`, `helper-*.spec.ts` |
| Post-call, rubrics, scores, tasks, notes | `test_feature_postcall.py`, `test_feature_models.py` | `rubrics.spec.ts`, `followthrough.spec.ts` |
| Coaching, library, replay, strategies | `test_feature_postcall.py`, `test_feature_models.py` | `test_coach_replay.py`, `test_strategy_policy.py`, `coaching.spec.ts`, `strategy.spec.ts` |
| Preparation and research | `test_feature_preparation.py` | `test_script_grounding.py`, `remediation.spec.ts`, `knowledge.spec.ts` |
| Statistical ML, training, experiments, learning | `test_feature_learning.py`, `test_feature_models.py` | `test_adaptive_model.py`, `test_training_isolation.py`, `test_pure_serving.py`, `test_conversation_associations.py`, `optimization.spec.ts`, `conversation-learning.spec.ts` |
| Numbers, health, capacity, staffing | `test_feature_operations.py` | `test_capacity_conservation.py`, `test_time_capacity.py`, `test_optimization_numbers.py`, `numbers.spec.ts` |
| Script learning: section reactions, effects on close, Markdown memory, tested script changes | `test_feature_script_learning.py` | `test_script_learning.py`, `script-memory.spec.ts`; plan `docs/SCRIPT_LEARNING.md` |
| Metrics, reports, work time | `test_feature_learning.py`, `test_feature_live_helper.py` | `report-projections.spec.ts`, `worktime.spec.ts`, `test_sqlite_plans.py` |
| Costs and billing | `test_feature_operations.py` | `billing-resolution.spec.ts` |
| Admin: settings, keys, users, backup/restore, ingress, tunnel, offline viewing | `test_feature_operations.py`, `test_feature_workspace.py` | `restore.spec.ts`, Rust tunnel tests in `lib.rs`, `cache-boundary.test.ts` |
| Provider HTTP contracts | - | `test_telnyx_contracts.py`, `test_providers.py`, `test_oauth.py` |
| Packaging and versions | - | `tests/test_distribution_contract.py`, `tests/test_package_macos.py` |

## 8. Test seams and conventions

- **Acceptance tests are HTTP-level.** Use `journey.py` (`call`, `get`, `post`, `run_jobs`, `event`, `answered_by_human`,
  `hang_up`, `wrap_up`, `import_leads`) and conftest's `start`, `reserve`, `ready`. The `workspace` fixture is a fresh
  simulated local workspace as owner (`operator@example.test` / `a-test-only-password-123`), seeded with three leads
  (415, 212, 512 area codes), two numbers (415, 212) and a national registry snapshot for 415/212/512.
- **Clock**: the `clock` fixture is mutable (`clock.now += timedelta(...)`). Sessions last 8 hours; after a long jump,
  sign in again (`POST /v1/auth/session`).
- **Background work**: `run_jobs(ws)` drains job lanes through `POST /v1/admin/jobs/run`. It does **not** run the
  maintenance tick (recovery, scheduled DNC refresh, preparation ahead); call
  `ws["client"].portal.call(app.state.service.maintenance_tick)` for that.
- **Models**: `install_fake_models(app, monkeypatch, script)` in `test_feature_models.py` is the seam for live help,
  finalization and reviews. It also turns simulation off, so start calls before installing it (or switch
  `settings.simulation` back for new calls).
- **Provider HTTP**: patch the shared client with `httpx.AsyncClient(transport=httpx.MockTransport(handler))`
  (`service.http`), or patch `app.research.fetch.httpx.AsyncClient` for site research. See the scheduled-refresh and
  research journeys.
- **Simulation mode** skips real-provider branches (`if not self.settings.simulation`). Those branches need a fake
  provider plus `settings.simulation = False`; several are still untested (section 9).
- **Browser specs share the two e2e workspaces** for the whole run. Many tests use fixed lead names or named
  workspaces, so `--repeat-each` fails on the second repetition by design. The shared fixture (`tests/fixtures.ts`)
  ignores route handlers whose request the UI cancelled and cleans up after failures.
- **Background refreshes race tests**: the e2e authorities run their workers, so a refresh can land between a test's
  setup and its next click. Wait for the state you depend on, or accept the refresh that arrives first.
- SQLite plans use fixed statistics (`db.plan_statistics`). Index creation order follows Python set order (hash seed);
  `test_sqlite_plans.py` guards the partial-index choice. Run perf checks with more than one `PYTHONHASHSEED`.

## 9. Known gaps: where to test next (priority order)

1. **Install the current signed release on a clean test machine** (not the owner's main machine): Mac DMG and Windows MSI/NSIS. First
   launch, local and simulated workspaces, a full simulated call, quit and relaunch (data persists), a 0.3 data folder
   left in place (0.4/0.5 must start fresh and leave it untouched), auto-update metadata. Not done yet.
2. **Rerun hosted Compose on 0.5.0 when Docker is available.** The completed October 24 Compose smoke was on 0.4.0, including restart persistence and plain refusal of a synthetic 0.3 marker. Production TLS, a dedicated host and real backups still need separate evidence.
3. **Remaining coverage gaps** (percentages are historical 0.4 measurements; coverage was not remeasured for 0.5):
   - Explicit number-recovery actions, verified configuration resubmission and signed callback confirmation
     (`_claim_number_check`, `confirm_callback_in`, `_allow_configuration_retry`, `reconcile_number_configuration`).
     Inventory pagination and basic configure/same-intent reconciliation/rejection now have mocked HTTP tests.
   - ChatGPT-plan account-switch and refresh identity rejection variants, and the backend `GET /auth/callback`.
     Exchange, successful refresh/inference and scope revocation have mocked coverage; real account eligibility is unproven.
   - Restore reconciliation of live calls against the carrier (`administration.py:reconcile_restore` → `finish`) and the
     webhook ingress check completion (`administration.py:_ingress_finish`).
   - Metered replay against a real coach (`optimization/api.py` `MeteredReplay`), streamed private-help publication
     with a real provider (`coaching/workflows.py` `publish`), and help withdrawal (`_withdraw_help`).
   - The research workflow web-search fallback (`intelligence.py`, `provider.search`). Redirect/domain/content/size
     limits now have explicit mock-transport tests.
   - The lane runner (`runtime.py`) is exercised only by the browser suite and compose smoke.
   - Low-coverage modules: `postcall/dates.py` (62%), `accounting.py` (69%), `telephony/recovery_actions.py` (65%).
4. **Hosted latency on a dedicated host**: `bench.py --hosted` against a real 2-4 vCPU server; target next-lead p95
   under 250 ms with 8 reps at one call per minute.
5. **Accessibility and small windows**: only partly covered (`completion.spec.ts` small-window keyboard journey).
6. **Windows-specific behavior**: CI runs the backend on Linux and builds Windows installers, but no Windows runtime
   tests run any more (the Windows acceptance workflows were removed in the lean plan).

## 10. Live gates (need the owner, accounts and spend)

None of these is proven. Do them only with the owner's explicit approval and their accounts:

| Gate | Needs | Evidence to collect |
|---|---|---|
| Live Telnyx calling | Telnyx API key, connection ID, public key, owned numbers, HTTPS webhook route (SSH forward or server) | Real outbound call bridged to the rep's WebRTC leg; signed webhook receipts; hangup; keypad; opt-out by DTMF 9; mailbox detection and hangup |
| Inbound return calls | Same, plus callback routing destination | Rep, teammate, business fallback and spoken-message routes on real calls |
| Hardware audio | Mic/speaker on Mac and Windows | Audio quality, device switching, mute |
| Live helper quality and latency | Funded OpenRouter key (Cerebras route, zero data retention) | Tip relevance and grounding on real conversations; latency against the targets |
| Research and scripts | OpenAI key or compatible endpoint, or a ChatGPT plan | Facts extracted from real company sites; script quality; costs |
| DNC vendor | donotcalldnc.com key with credits; seller registry authorization | Real lookups, charges matching estimates, scheduled refresh within budget |
| Provider retention terms | Account settings / contracts | Written confirmation of retention for each provider |
| Hosted production | A real server | Deploy, TLS, backups, restore, latency |
| Campaign lift | Real campaigns over weeks | Outcome comparison; the app only reports observational and controlled-comparison evidence |

## 11. Decisions and limitations (not bugs)

- No upgrade path from 0.2/0.3; fresh stores only.
- Provider keys are stored as plain JSON on the authority (internal use).
- Buying numbers, call recording, subscriptions and CRM/calendar sync stay disabled by policy.
- Raw speech, quotes and helper text expire within five minutes; there is no recording.
- Size targets were missed: backend -14% (target about -30%), frontend -2% (target about -20%). See HANDOFF_AST.md.
- The strategy policy keeps the reviewed default unless a supported approach estimates better (fixed in `29034c0`).

## 12. What changed recently (lean plan, October 22-23, 2026)

Full log: `git log --oneline a950675..HEAD`. Highlights:

- Plain storage (no encryption, journals, Alembic or legacy upgrades); reports computed directly from source records.
- Simpler job runner, priority gate and SQLite writer lock; next lead ranks and claims in one transaction (15 SQL
  statements); carrier webhooks apply in arrival order (fixed a PostgreSQL race).
- Desktop shell reduced to sidecar launch and webhook tunnel; UI keeps sessions and offline copies in local storage.
- SQLite partial-index statistics fixed (workspace reads were sometimes ~1 s).
- 0.4.0: new desktop data folders, plain refusal of 0.2/0.3 databases.
- Browser journeys made robust to background refreshes.
- Restored tests for scheduled DNC refresh, conversation associations, coaching strategies, site research, helper
  metrics and the desktop launch handshake; they caught the strategy-policy defect.

## 13. Documents: current and history

Current: this file, [HANDOFF_AST.md](HANDOFF_AST.md), `AGENTS.md`, [LEAN_PLAN.md](LEAN_PLAN.md) (plan and Status),
[VALIDATION.md](VALIDATION.md) (top section), [RELEASE_NOTES.md](RELEASE_NOTES.md) (0.5.0), `README.md`,
`deploy/runbooks/*`, `docs/evidence/lean/*`.

History (feature descriptions only; mechanisms and numbers there are out of date): `REMEDIATION_PLAN.md`,
`EXECUTION_SPEC.md`, `EXECUTION_TASKS.json`, `STATUS.md`, `PLAN.md`, `OPTIMIZATION_PLAN.md`, `ACCEPTANCE_MATRIX.md`,
`VALIDATION_0.1.1.md`, the 0.3.3 part of `VALIDATION.md`, `ARCHITECTURE_REVIEW_2026-10-18.html`.

## 14. Reporting back

Report to the owner in plain language: what you tested, what passed with the command and output, what failed with
the test that reproduces it, what you fixed and which commit, and which live gates remain. Keep "verified with
simulated providers" separate from "verified live".
