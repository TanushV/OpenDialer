# OpenDialer 0.2.0 acceptance

Runtime source is `d6622149e8f3f2cb5114304e045cc1506b99ddbc`; acceptance-source revision is `c8f7bd8fe123d0022af53126eb81fc00473ce1e2` (the later commit corrects only a fixture and execution notes). Both native packages include the same accepted application code. The original specification and [optimization checklist](OPTIMIZATION_PLAN.md) are preserved. The [0.1.1 record](VALIDATION_0.1.1.md) remains available.

| Surface | Observed evidence |
|---|---|
| TDD | New acceptance cases preceded production changes; subsequent allocation, delayed-label/cohort, semantics, provider, feedback, cold-start and metrics fixes have actual failing and passing receipts in the execution log. |
| SQLite | 280 tests pass; 83% overall backend statement coverage. This is broad acceptance coverage, not 100% branch coverage or proof of live behavior. |
| PostgreSQL 17 | 271 pass; one SQLite-only byte-inspection check is skipped. The domain, encryption, scope, claims, API and recovery suite runs against a real disposable database. |
| Frontend | 11 unit tests, strict TypeScript/production build, formatting and seven browser workflows pass. Live events, contextual cards, corrections, saved optimization settings and connected login are exercised. |
| Native boundary | Four Rust tests, formatting and clippy with warnings denied pass. Native application CI and Windows installer acceptance are recorded below after their runs finish. |
| Packaged backend | The final signed Mac executable passes fresh-directory startup, encrypted setup, calling, grounded budget guidance, feedback, conversion/retraction, dataset/comparison routes and preflight without a source checkout or installed Python runtime dependency. |
| Cold startup | A packaged first-call timeout was caught before publication. Baseline calling/planning now avoid loading optional ML libraries; a regression blocks that import and the packaged test passes with its original deadline. |
| Statistical integrity | Immutable pre-call snapshots, original/uncalled/pending denominators, independent chronological calibration, stage/subgroup samples, owned training, fixed control, bounded reviewed pilot/expiry, real model exposure, reviewed promotion and rollback are tested. Refusal/opt-out does not imply right-party identity; sale does not invent a meeting. |
| Guidance | MockTransport verifies Cerebras-only OpenRouter routing, required JSON schema, no provider fallback and actual-route/usage receipts. Exact approved meaning and quoted evidence are validated; revisions, cancellation, cooldown, TTL, fallback and partial feedback are tested. State/retrieval/inference/render boundaries remain separate; unknown STT/full-path latency is explicit. |
| Deployment | The non-root connected container is rebuilt from accepted code, explicitly migrated and restarted against a separate preserved PostgreSQL workspace. Its scoped login, optimization report and preflight pass. |
| Security | Python/npm dependency scans report no known vulnerabilities. The Cargo OSV findings are limited to Linux-only transitive packages absent from the Mac ARM64/Windows x64 trees. Linux is outside this preview. Tracked files contain no private keys, databases or packaged executables. |
| Mac integrity | Complete-bundle strict/deep codesign passes. Pinned-key archive signature, authenticated 0.2.0 version and deliberate-tamper rejection pass. The app remains ad-hoc signed; public publisher trust/notarization is a separate gate. |
| Windows distribution | Pending the exact-source installer/native job. NSIS/MSI bytes and signatures must pass verification before combined publication. |
| Native update continuity | Pending signed in-app updates from 0.1.1. Preserve existing IDs, nonempty human notes, suppressions and protected sessions; never uninstall/reset. |

No real carrier call, phone-number purchase, funded inference or message was performed. Fictional sample people and simulated providers are visibly labeled. The initial 2.5-second p95 useful-card objective, provider account eligibility/traffic approval, hardware audio/NAT/Bluetooth/sleep behavior, real replay quality, production hosting and measured campaign lift remain unverified without those resources. Unconfigured live workflows fail closed where required; approved static help and isolated simulation work without keys.
