# OpenDialer lean build validation

**Current verification: October 24, 2026, script-learning corrections `74658b4`.**
[Current handoff](HANDOFF.md) and [receipt](evidence/lean/script-learning-fixes-20261024.json) record the results:
628 SQLite / 629 PostgreSQL tests, 155 browser cases, 74 frontend tests and 7 Rust tests pass,
as do lint/format/typecheck/build/clippy, fresh sidecars and Mac package/archive/restart verification.
Four failing regressions preceded fixes for mutable trial controls, stale-winner promotion and cached scripts
ignoring changed memory. The browser suite passed a fresh rerun after two interaction failures under concurrent
backend/build load. Installed app/data were untouched; updater signing used a deleted disposable key.
Production signing, notarization, clean-machine installation, hardware audio and live providers remain unverified.
Performance, coverage and compose results below are earlier snapshots, not reruns of these corrections.

**Earlier October 24 verification snapshot, product fix `538dcde`, browser regression `f9eacca`.**
[Current handoff](HANDOFF.md) and [verification receipt](evidence/lean/verification-20261024.json) contain the
commands, counts, regression evidence and remaining gaps. The [Mac/local CI receipt](evidence/lean/mac-local-ci-20261024.json)
records the pre-push verification requested by the owner. Installed app and data were not accessed or modified.
GitHub results are tracked in [Actions](https://github.com/TanushV/OpenDialer/actions/workflows/ci.yml).

| Check | October 24 result |
| --- | --- |
| Backend | Fresh full suites: 600 passed / 2 skipped on SQLite; 601 passed / 1 skipped on PostgreSQL. Backend line coverage 84.8%. Optional Caddy ingress tests separately enabled: 2 pass. |
| Browser | Final fresh full run: 154/154 pass (6.3 minutes). Initial run: 153 pass, one repeated-conflict test assumption failed. Deterministic reproduction and an explicit review/retry test fix preserve the app's conflict checks. |
| Frontend / Rust | Typecheck, format, production build, 74 Vitest tests; Rust format, 7 tests and clippy all pass. |
| Product fix | ChatGPT-plan streaming rejects non-object JSON. Arrays, null, strings and numbers all failed the new regression tests before the fix and pass afterward. |
| Local performance | Two hash seeds, each 10k leads / 100k attempts / 40 samples. All local targets met: next lead p95 47.8-53.5 ms, 15 SQL statements; webhook receipt 8.3 ms; event to state 11.9-14.3 ms; wrap-up 15.2-22.5 ms; workspace 34.0-57.1 ms; startup 61-78 ms. |
| Hosted deployment | Fresh 0.4.0 compose build: bootstrap, 20-lead import, separate-worker preparation, call/callback and restart persistence pass, with no runtime error lines. A synthetic 0.3 marker is refused without changing the logical PostgreSQL dump. |
| Hosted workload | Eight reps x one call/min for five minutes, 2,000 leads: 40/40 journeys, zero failures. p95 next lead 141.1 ms, event-to-state 224.3 ms, wrap-up 198.2 ms. Uncapped shared Mac with separate API/background processes and Docker PostgreSQL; not certification of the dedicated 2-vCPU reference. |
| Frozen sidecars | Both rebuilt. Authority passes a simulated call/callback and restart; trainer fits a human stage from 600 synthetic rows and exports a checksum-valid artifact. This does not verify the installer or hardware audio. |
| Mac packaging | Current app builds and passes deep strict signature and extracted archive verification; both sidecars run under the hardened runtime. Extracted authority passes simulated call/callback and restart; trainer imports pass. DMG creation/checksum pass. Updater signature uses a disposable test key, not the production key; no notarization or installation. |
| Remaining gates | Live Telnyx/inference/DNC accounts, hardware audio, clean-machine Mac/Windows installers, provider terms/legal approval, production hosting and dedicated-host latency, campaign lift. Backend and frontend size targets remain missed. |

The older signed draft predates the inference fix and was not rebuilt or published. Mocked providers and synthetic
training prove software behavior only; they do not establish live provider compatibility, account eligibility or lift.

## October 23 verification snapshot

**Status: 0.4.0 lean build on `main` (October 23, 2026). A signed draft release is built from `a05a196` ([run 40066009752](https://github.com/TanushV/OpenDialer/actions/runs/40066009752)) and not published.** It does not read 0.2/0.3 workspaces: the desktop opens a fresh one in `local-2/` and leaves the old folders untouched; a server refuses a 0.2/0.3 database. Plan and step receipts: [LEAN_PLAN.md](LEAN_PLAN.md).

| Gate | Result |
| --- | --- |
| Feature acceptance | `services/backend/tests/acceptance/` drives every kept feature over HTTP (103 journeys) and passes on SQLite and PostgreSQL. |
| Backend suites | 579 passed on SQLite and 580 on PostgreSQL (acceptance, unit and repo tests). GitHub CI green. |
| Browser | Playwright journeys in local (SQLite) and connected (PostgreSQL) modes pass; on a heavily loaded Mac a few connected steps need the 10 s expectation / 60 s test timeouts now configured. |
| Desktop shell | Rust fmt, tests and clippy pass; the shell only launches the local service and runs the SSH webhook forward. |
| Packaging | Sidecars freeze; the frozen authority runs 200 simulated calls; the frozen trainer trains the challenger; the 0.4.0 Mac bundle seals, verifies and starts both sidecars under the hardened runtime and passes archive verification (the updater signature is applied by the release workflow). |
| Old installs | The desktop keeps 0.4.0 data in new folders; a 0.2/0.3 database is refused with a plain message and left unchanged (checked on a real 0.3.3 store). |
| Hosted topology | Compose smoke passes: migrate job, API, background worker, PostgreSQL; bootstrap, import, worker preparation, a rep call through wrap-up with a callback task, restart persistence, no error lines (`evidence/lean/hosted-smoke.json`). |
| Local speed | 10k leads / 100k attempts, baseline and lean back to back on the same shared Mac: every hot path 2.5-4x faster (next lead p95 176 -> 80 ms, wrap-up 69 -> 33 ms, workspace 204 -> 71 ms, startup 359 -> 74 ms) and every local target met, including 15 SQL statements for next lead (`evidence/lean/bench-local.json`). |
| Hosted speed | 2-vCPU reference shape, 8 reps at one call per minute for 5 minutes, no failures in two runs on a shared Mac: event to state p95 85-209 ms and wrap-up p95 96-270 ms; next lead p95 0.8-1.2 s misses the 250 ms target (p50 100-200 ms). Docker CPU caps on that host do not reserve CPU; size a dedicated host and measure with `scripts/bench.py --hosted` (`evidence/lean/bench-hosted-reference.json`). |

Simulated providers prove software behavior only. Live Telnyx traffic, funded Cerebras quality and latency, DNC vendor access, provider retention terms, hardware audio and campaign lift remain external gates.

# OpenDialer 0.3.3 validation

**Status: developer preview, published October 21, 2026 as [v0.3.3](https://github.com/TanushV/OpenDialer/releases/tag/v0.3.3). The Windows in-place update from 0.2.1 is accepted; the Mac in-place update is pending.** Source `beb3cf3b220e4f0435f5e9fc9bb29578109a1463`; product code is 0.3.0 plus three fixes found by validating the update from 0.2.1. Execution evidence is in [STATUS.md](STATUS.md) and [evidence/completion](evidence/completion/).

**0.3.0, 0.3.1 and 0.3.2 were withdrawn.** Each was published as a controlled validation release, failed the signed Windows update from 0.2.1, and was then marked prerelease so the updater manifest returned to 0.2.1. Installed data stayed intact throughout.
- 0.3.0 (19:34–20:31 UTC) installed the exact bytes, but the updated app could not open the migrated workspace: the upgrade flushed its retained recovery copy through a read-only handle, which Windows rejects and macOS accepts (`E14-windows-upgrade-flush.json`).
- 0.3.1 (21:18–21:37 UTC) opened the migrated workspace with every ID and the human note, and passed the local journey and resource gates, but could not sign out: 0.2.1 had left simulated prospect legs open behind ended calls (`E14-legacy-prospect-legs.json`).
- 0.3.2 (22:23–22:45 UTC) passed the MSI update end to end. On NSIS, raw speech stayed on screen past six minutes: the desktop's expiry timer could fire while both of its clocks still read live, and nothing rescheduled it (`E14-raw-expiry-timer.json`).

| Gate | Result |
| --- | --- |
| Software suites | Green CI (run [39691089984](https://github.com/TanushV/OpenDialer/actions/runs/39691089984)) on a head with the release's production tree: SQLite suite 2941 passed / 34 skipped; PostgreSQL push subset 122 passed; coverage 93.6% statements / 85.3% branches (floors 90/80); 156 browser journeys in local and connected PostgreSQL modes; 78 frontend unit tests; Rust native tests; lint and dependency audits; and the Windows storage job (upgrade, migration, lock and journal suites on Windows). |
| Local performance | 10k leads / 100k attempts, run alone (`E13-local-edc48e3.json`). Every gate passes: warm next lead 23.5 ms p95 (25 SQL); selection under load 68.5 ms; event acknowledgement 6.4 ms; event to state 37.5 ms; hangup 47.7 ms; wrap-up 34.2 ms; writer 21.6 ms; startup 1.2 s. |
| Scaling | 100k leads / 1M attempts (`E13-scaling-0c094f7.json`). Hot paths pass: warm next lead 21 ms, workspace 17 ms. The explicit full rebuild takes about 45 minutes, which exceeds the harness bound. |
| Connected server | Hosted mode works on the shipped compose topology at the current source: migrate job, API with live lanes, separate background worker and PostgreSQL pass setup, worker preparation, a rep call through wrap-up, and a restart that keeps outcome and task (`E13-hosted-smoke-7d571d1.json`). Under load, correctness holds: zero write conflicts and zero background errors at 2 and 8 reps, at one call per rep every 10 s and every 60 s. Load-path latency is not certified on the 2-vCPU reference at any tested point, down to two reps at one call per minute (selection p95 1.18 s; `E13-shared-2reps-60s-7d571d1.json`). These measurements run every lane in one process on a VM shared with unrelated workloads. Size a dedicated host and run the capacity search there (deploy runbook). |
| Packaged runtime | The frozen Mac and Windows authorities and trainers pass the 12-minute journey, raw expiry, restart and training-child acceptance without a source checkout (release run [39689276616](https://github.com/TanushV/OpenDialer/actions/runs/39689276616)). |
| Package integrity | The Mac archive, NSIS and MSI pass pinned-key signature, authenticated version 0.3.3 and deliberate-tamper rejection. The published `latest.json` and `SHA256SUMS.txt` were regenerated from, and match, the uploaded bytes; the Windows packages are the exact bytes that passed native acceptance (staging run [39693998949](https://github.com/TanushV/OpenDialer/actions/runs/39693998949)). |
| Windows fresh install | Run [39689279665](https://github.com/TanushV/OpenDialer/actions/runs/39689279665) installs NSIS into a clean runner. The installed app passes the local and connected journeys, each with a real five-minute raw expiry (314 s and 315 s), wrap-up, review correction, follow-up with the callback time kept, native restart with a restored session, next-call context, capacity and preflight. Resources over a 300 s idle window: steady desktop 233.5 MiB (543.6 MiB post-journey peak reported), authority 131 MiB, idle CPU 0.82%. Startup 10.1 s on the first launch of a fresh install, then 5.7 s and 1.9 s. |
| Windows in-place update | Accepted. Run [39708283954](https://github.com/TanushV/OpenDialer/actions/runs/39708283954) installed the published 0.2.1 on fresh runners, created the baseline through 0.2.1's own UI, and let its signed updater install 0.3.3, NSIS and MSI separately. Each waited for the updater to exit and for the exact accepted backend (`cb67c5b5…`), reopened the migrated workspace with every baseline ID and the identical human note, and passed the local and connected journeys: raw expiry at 314.5–315.3 s, restored sessions, kept callback times, source corrections, capacity, DNC/cost, recovery, sign-out and preflight. Resources: steady desktop 237 MiB (NSIS) and 230 MiB (MSI), authority 131 MiB, idle CPU 0.68–0.69%. NSIS also passed in runs 39694452712, 39696124396 and 39698794815. The WebDriver session resets the WebView profile, so a 0.2.1 session kept in WebView storage cannot be observed; session records are preserved and 0.3.3's own sessions restore. Receipt: `WINDOWS_UPDATE_ACCEPTANCE.json` on the release. |
| Mac in-place update | Pending. The installed Mac (0.2.1) receives 0.3.3 through Settings → App updates; a person must click Install, because UI scripting is not permitted on that Mac. A read-only snapshot of its stores was taken beforehand, and `work/remediation/completion/mac_update_check.py after` checks continuity once it updates. |
| Publisher trust | Updater integrity signatures only. Apple Developer ID/notarization and Windows publisher signing are not configured, so expect trust prompts on a fresh install. |

No real carrier call, number purchase, funded inference or message was performed. Live Telnyx traffic, funded Cerebras quality and latency, DNC vendor access, provider retention terms, hardware audio and measured campaign lift remain external gates (X01–X08).

# Historical OpenDialer 0.2.1 acceptance

Released application source is `141c73b1d3925ef652d2a8f981f3455d53159c53`; its unchanged backend was frozen from `05c08bf6e55f9752275180f3c0b59779c19232de`. Later commits contain acceptance-harness and documentation changes. The [optimization checklist](OPTIMIZATION_PLAN.md), [execution log](STATUS.md), preserved specification and [0.1.1 record](VALIDATION_0.1.1.md) retain the requirements and red/green history.

Mac and Windows packages are published together in [v0.2.1](https://github.com/TanushV/OpenDialer/releases/tag/v0.2.1). The existing Mac was updated through its signed updater, without reinstalling or resetting data. Windows passed both fresh NSIS installation and the published 0.1.1 baseline's actual signed in-app upgrade. The withdrawn 0.2.0 candidate is retained as a draft for diagnosis.

| Surface | Observed evidence |
|---|---|
| TDD | Acceptance cases preceded production changes. Allocation, delayed-label/cohort, semantics, provider, feedback, cold-start, metrics and native cleanup fixes retain actual failing and passing receipts. Nine further updater-window/completion harness cases pass. |
| SQLite | 280 acceptance cases pass; 83% overall backend statement coverage. This is broad software coverage, not 100% branch coverage or live-provider proof. |
| PostgreSQL 17 | 271 pass; one SQLite-only byte-inspection check is skipped. Domain, encryption, scope, claims, API and recovery cases run against a real disposable database. |
| Frontend | 11 unit tests, strict TypeScript/production build, formatting and eight browser workflows pass. Tests cover live events, contextual cards, corrections, optimization persistence, connected login and packaged-version display. |
| Native boundaries | Five Mac and four Windows Rust tests pass, including cleanup before the updater's early exit. The exact-path PowerShell cleanup-scope test passes. Formatting and Mac clippy with warnings denied pass. Full application/native CI [38890758574](https://github.com/TanushV/OpenDialer/actions/runs/38890758574) is green for released source. |
| Packaged backend | The actual final signed Mac executable passes fresh-directory startup, encrypted setup, calling, grounded budget guidance, feedback, conversion/retraction, dataset/comparison routes and preflight without a source checkout or Python installation. |
| Cold startup | A packaged first-call timeout was caught before release. Baseline calling/planning avoid optional ML imports; a regression and the packaged workflow pass with their original deadline. |
| Numbers | Observed health/missingness, case-mix adjustment, uncertainty, stable identities, usable/pending/warmup capacity, claims, regional demand, bounded quotes and uncertain-order recovery have domain/API checks. Purchases and external reputation are not manufactured by simulation. |
| Statistical integrity | Immutable pre-call features, original/uncalled/pending denominators, independent chronological calibration, stage/subgroup samples, owned training, fixed control, bounded pilot/expiry, actual model exposure, reviewed promotion and rollback are tested. Refusal/opt-out does not imply right-party identity; sale does not invent a meeting. |
| Guidance | MockTransport verifies Cerebras-only OpenRouter routing, required JSON schema, no provider fallback and actual-route/usage receipts. Approved meaning and quoted evidence, revisions, cancellation, cooldown, TTL, fallback and partial feedback are tested. Unknown STT/full-path latency remains explicit. |
| Connected authority | The non-root container was rebuilt, migrated and restarted against a separately preserved PostgreSQL workspace. Scoped login, optimization views, native preflight, protected-session recovery and the saved human note pass. Disposable services were removed after acceptance. |
| Security | Python/npm dependency scans report no known vulnerabilities. Cargo OSV findings are in Linux-only transitive packages absent from active Mac ARM64/Windows x64 trees; Linux is outside distribution. Private keys, databases and build products are outside Git. |
| Mac distribution | Strict/deep bundle codesign, pinned-key archive signature, authenticated 0.2.1 version and deliberate-tamper rejection pass. Packages are ad-hoc signed; trusted publisher signing/notarization is a separate gate. |
| Windows distribution | Exact-source build [38890758285](https://github.com/TanushV/OpenDialer/actions/runs/38890758285) passes signed NSIS/MSI packaging, clean NSIS install and actual WebView2 calling/opt-out/review/restart/optimization/preflight. MSI is built and signature-verified; native workflows use NSIS. Staging [38892974833](https://github.com/TanushV/OpenDialer/actions/runs/38892974833) verifies signatures, version, tamper rejection and uploaded digests without rebuilding. |
| Mac update continuity | Actual 0.1.1→0.2.0→0.2.1 signed updates retain original records. The final 0.2.0→0.2.1 update preserves exact current IDs, human-note hashes, opt-outs and encrypted payloads. Local/connected native preflight report 0.2.1; the protected connected session and note survive full restart. Normal quit leaves no app-owned processes. |
| Windows update continuity | [38895774436](https://github.com/TanushV/OpenDialer/actions/runs/38895774436) is green: install the unchanged published 0.1.1 baseline once, create a note/opt-out, use its actual signed updater, await installer exit and exact packaged backend bytes, reopen the same path, and verify 0.2.1 backend preflight, exact saved IDs and visible human note. Earlier harness failures remain documented; GUI version alone never closes this gate. |
| Final preflight | Installed SQLite integrity/foreign keys pass, exact saved baseline matches, no sample claims remain, and the app is paused on local 0.2.1. Local preflight still passes after disposal of the task-owned API/PostgreSQL containers, anonymous volume, network, image tags and fake authority environment. Unrelated running services are preserved. |

The release contains SHA256SUMS, SOURCE_REVISION, MAC_NATIVE_ACCEPTANCE, WINDOWS_NATIVE_ACCEPTANCE, WINDOWS_UPDATE_ACCEPTANCE and WINDOWS_VERIFICATION receipts. Application binaries are unchanged during acceptance reruns.

No real carrier call, phone-number purchase, funded inference or message was performed. Fictional sample people and simulated providers are visibly labeled. Live account/traffic approval, real media and hardware audio/NAT/Bluetooth/sleep behavior, representative replay quality, complete STT-to-card latency, production hosting/publisher trust and measured campaign lift still require their own evidence. Unconfigured live workflows fail closed where required; isolated simulation and approved static help work without keys.
