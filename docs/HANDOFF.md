# OpenDialer testing handoff (CURRENT)

> **Status: CURRENT** handoff as of **October 23, 2026**, describing commit `e373a9c` on `main` plus the commit that adds
> this file. It is the single handoff to read first. If a newer handoff exists, it will say so at its top and this
> one will be marked superseded. The companion code map is [HANDOFF_AST.md](HANDOFF_AST.md) (sizes, all HTTP
> routes, every backend module's classes and functions, generated from the AST).

You are taking over **testing** of OpenDialer 0.4.0. The code is feature-complete for the approved plan, every suite
is green, and a signed draft release exists. Your job is to find what the existing tests do not prove, add tests,
and fix defects they reveal, then prepare and support live testing. Read this file, then `AGENTS.md`, then the
Status section of `docs/LEAN_PLAN.md`.

## 1. What OpenDialer is

A desktop calling workspace for small internal sales teams. One FastAPI authority (`services/backend/app`) serves a
React/TypeScript UI (`apps/desktop/src`) inside a small Tauri shell (`apps/desktop/src-tauri`). Two modes, same code:

- **Local**: the desktop launches a bundled authority on a random loopback port with SQLite in its data folder.
- **Hosted (connected)**: a shared server on PostgreSQL (`deploy/compose.yml`: migrate job, API, background worker,
  PostgreSQL, Caddy). Desktops sign in to it.

Providers: Telnyx (calls, WebRTC agent leg, numbers, signed webhooks), Cerebras through OpenRouter (live helper and
rundowns), OpenAI / OpenAI-compatible endpoint / a rep's ChatGPT plan (research and scripts), donotcalldnc.com
(prepaid DNC lookups). **Simulation mode** replaces all of them with simulated providers and a fixed clock.

## 2. State at handoff

| Item | State |
|---|---|
| Version | 0.4.0 everywhere the distribution contract checks (`tests/test_distribution_contract.py`) |
| `main` | `e373a9c` (+ this handoff), clean, pushed. CI green on every job (lint, backend SQLite, backend PostgreSQL, frontend, e2e, desktop shell) |
| Backend + repo tests | 579 passed on SQLite, 580 on PostgreSQL (a few skips need a Caddy image or a guarded store) |
| HTTP acceptance journeys | 103 in `services/backend/tests/acceptance/` |
| Browser journeys | 154 Playwright journeys, local (SQLite) and connected (PostgreSQL) modes, all passing locally and in CI |
| Frontend unit / Rust | 74 Vitest tests; Rust fmt, 7 tests, clippy pass |
| Backend line coverage | 84.3% (gaps listed in section 9) |
| Draft release | **v0.4.0, signed, UNPUBLISHED**, built from `a05a196` by [run 40066009752](https://github.com/TanushV/OpenDialer/actions/runs/40066009752): Mac archive + DMG (aarch64), Windows NSIS + MSI (x64), updater signatures, `latest.json`, `SHA256SUMS.txt`. `main`'s app code is identical to `a05a196` (later commits are docs only). v0.3.3 is still the latest published release |
| Packaging | Both sidecars freeze; frozen authority ran 200 simulated calls; frozen trainer trained a model; the Mac bundle seals, verifies and starts both sidecars under the hardened runtime |
| Local speed | 10k leads / 100k attempts: next lead p95 50-80 ms with 15 SQL statements, wrap-up ~25-33 ms, workspace ~70 ms, startup < 0.1 s. All local targets met |
| Hosted speed | 2-vCPU reference shape, 8 reps x 1 call/min: no failures; next-lead p95 0.8-1.2 s **misses** the 250 ms target; measured on an oversubscribed Mac, so it needs a dedicated host |
| Hosted compose smoke | Passed at `a9a129c` (`docs/evidence/lean/hosted-smoke.json`). **Not rerun since 0.4.0**; rerun it |

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
9. Fresh stores only: 0.4.0 does not read 0.2/0.3 data (by decision). The desktop keeps 0.4.0 data in `local-2/` and
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

On the owner's Mac this container already exists and is running. If a browser or bench run fails with "This
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

About 2-3 minutes on SQLite and 4-13 minutes on PostgreSQL depending on machine load.

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
browser database) and the Vite dev server (1420). Workers = 1; the whole run takes 7-11 minutes.

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
errors. The last result is `docs/evidence/lean/hosted-smoke.json` (at `a9a129c`).

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

1. **Install the draft on a clean test machine** (not the owner's main machine): Mac DMG and Windows MSI/NSIS. First
   launch, local and simulated workspaces, a full simulated call, quit and relaunch (data persists), a 0.3 data folder
   left in place (0.4.0 must start fresh and leave it untouched), auto-update metadata. Not done yet.
2. **Rerun the hosted compose smoke on 0.4.0** (section 5.9), including the plain refusal of a 0.3 database.
3. **Untested code** (no test executes it; most are real-provider paths):
   - Telnyx number inventory listing (`providers/telnyx.py:list_numbers`) and number configuration/claim/callback
     checks (`number_operations.py`: `_claim_number_check`, `confirm_callback_in`, `carrier_rejection`,
     `_allow_configuration_retry`, `reconcile_number_configuration`). Contract-test them with recorded Telnyx
     responses through a mock transport.
   - ChatGPT-plan OAuth exchange/refresh/complete (`providers/oauth.py`) and `GET /auth/callback`.
   - Restore reconciliation of live calls against the carrier (`administration.py:reconcile_restore` → `finish`) and the
     webhook ingress check completion (`administration.py:_ingress_finish`).
   - Metered replay against a real coach (`optimization/api.py` `MeteredReplay`), streamed private-help publication
     with a real provider (`coaching/workflows.py` `publish`), and help withdrawal (`_withdraw_help`).
   - The research web-search fallback (`intelligence.py`, `provider.search`) and research redirect/size limits.
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
| Live Telnyx calling | Telnyx API key, connection ID, public key, owned numbers, HTTPS webhook route (SSH forward or server) | Real outbound call bridged to the rep's WebRTC leg; signed webhook receipts; hangup; keypad; opt-out by DTMF 9; final-attempt voicemail detached |
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
[VALIDATION.md](VALIDATION.md) (top section), [RELEASE_NOTES.md](RELEASE_NOTES.md) (0.4.0 section), `README.md`,
`deploy/runbooks/*`, `docs/evidence/lean/*`.

History (feature descriptions only; mechanisms and numbers there are out of date): `REMEDIATION_PLAN.md`,
`EXECUTION_SPEC.md`, `EXECUTION_TASKS.json`, `STATUS.md`, `PLAN.md`, `OPTIMIZATION_PLAN.md`, `ACCEPTANCE_MATRIX.md`,
`VALIDATION_0.1.1.md`, the 0.3.3 part of `VALIDATION.md`, `ARCHITECTURE_REVIEW_2026-10-18.html`.

## 14. Reporting back

Report to the owner in plain language: what you tested, what passed with the command and output, what failed with
the test that reproduces it, what you fixed and which commit, and which live gates remain. Keep "verified with
simulated providers" separate from "verified live".
