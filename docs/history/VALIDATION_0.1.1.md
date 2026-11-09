# Acceptance record

This is a software acceptance record for the 0.1.1 developer preview, executed on macOS Apple Silicon. The user explicitly requested proceeding without provider credentials. Simulation and HTTP contracts therefore establish software behavior; they do not establish live Telnyx, Groq, OpenAI, registry/legal or hardware acceptance.

| Surface | Verified evidence |
|---|---|
| TDD | Initial domain/API/provider/native/distribution/UI tests were committed before production code. The first run failed with missing app module. Later storage, stale coaching, final-note status, restore hold, batch workflow, callback, hangup and process-lifecycle fixes each have recorded red/green regressions. |
| Backend / SQLite | 180 acceptance tests pass; 78% backend statement coverage. Permission/security/reducer and important invariants have focused tests. This is not 100% branch coverage. |
| Backend / PostgreSQL | 179 pass and one SQLite byte-inspection skip against real PostgreSQL 17; concurrency, claims, durable jobs, tenant isolation and encryption share the domain suite. |
| Frontend | Eight state/cache/CSV tests, strict TypeScript with unused-code checks, production Vite build, formatting checks and five browser workflows. |
| Native | Four Rust tests: URL boundary, nonblocking credential I/O, authenticated namespace-bound cache encryption, and launcher/child process cleanup. Rust formatting and clippy with warnings denied pass. |
| Providers | MockTransport contracts for Telnyx, OpenAI Responses/official plan, LiteLLM and Groq; JWT/JWKS/PKCE OAuth contracts; signed webhook, response uncertainty, out-of-order events and command recovery fixtures. No unintended calls or purchases. |
| Research / analysis | Real HTTP fixture boundaries for pinned DNS/SNI, private redirects, size/type/domain/redirect caps; evidence and entity validation, batch partial reconciliation, final transcript revisions and preserved human edits. |
| Models / measurement | Chronological/grouped training, independent calibration, artifact checksum/path validation, policy/coverage/manual promotion gates, stable control, exact action propensities and censored delayed outcomes. No measured outcome lift is claimed. |
| Deployment | Final Docker image builds with a non-root runtime; explicit migration and updated container API retain the dedicated PostgreSQL acceptance workspace. This database is separate from reset-based test storage. Production Compose keeps PostgreSQL private, orders migrations and separates worker lanes. |
| Security dependencies | npm audit: zero vulnerabilities. pip-audit: no known vulnerabilities after cryptography 50.0.2 remediation. These are point-in-time scans, not a proof of absence of all defects. |
| Windows installation / native UI | NSIS/MSI built on Windows 2022 from source 61eada8; native acceptance run [38705124584](https://github.com/TanushV/OpenDialer/actions/runs/38705124584) passes against those unchanged installer bytes. Fresh NSIS install, real WebView2 call/review workflow, saved human note reopened after a new driver session, and database preflight pass. MSI is built and signature-verified; this native flow uses NSIS. |
| Release integrity | Mac archive and Windows NSIS/MSI pass verification with the pinned key, authenticated version 0.1.1 and deliberate-tamper rejection. GitHub's uploaded SHA-256 digests and platform manifest signatures match local bytes. The public latest endpoint serves the exact combined manifest. |

Cargo.lock was also checked against OSV. Two transitive Linux-only packages have advisories; neither is active in the Mac ARM64 or Windows x64 dependency trees. Linux packages are excluded from this preview. This is not a claim that the entire lockfile is advisory-free.

Mac native acceptance was performed through computer use in `/Applications/OpenDialer.app`. The existing 0.1.0 app checked the public latest endpoint and installed signed 0.1.1 through Settings → App updates. No uninstall, reinstall or application-data reset occurred. After restart, exact lead/call/note/suppression IDs, the nonempty saved human-note hash and active opt-out count matched the pre-update baseline. Existing payloads are now encrypted; SQLite integrity and foreign-key checks pass. The original note is visible in native review, and native preflight reports 0.1.1 / local.

The updated app then signed in to an actual non-root container API backed by a separate PostgreSQL 17 database. Native readiness, reservation, simulated call/answer/transcript, approved static coach, hangup, callback disposition and human-note save succeeded. The authenticated API and encrypted PostgreSQL row reconcile the same call and exact note. A full native quit/reopen restored the protected connected session and displayed the same human note; connected preflight reports 0.1.1 / connected. Quitting the updated app terminated the GUI, sidecar launcher and frozen runtime child without an orphan. The app was returned to its preserved local simulation and paused. Temporary API/PostgreSQL containers, their disposable volume/network and task-owned Docker image tags were removed after acceptance; the installed app and its data remain.

Application/native CI at [a596fe3](https://github.com/TanushV/OpenDialer/actions/runs/38705125063) and installed Windows native acceptance at [38705124584](https://github.com/TanushV/OpenDialer/actions/runs/38705124584) are green. Release binaries use production source `61eada8a5e6e0db0ff6cb539afd537724c14f357`; subsequent commits correct only the acceptance harness and documentation. See [release 0.1.1](https://github.com/TanushV/OpenDialer/releases/tag/v0.1.1) for packages, signatures and checksums.

## Remaining external acceptance gates

- Actual carrier traffic approval, per-rep direct-call bypass attempt, caller-ID/bridge/AMD/voicemail topology, callback audio and carrier/invoice billing receipts.
- Live Soniox/Parakeet timing/attribution, Groq/model quotas and end-to-end STT-to-card latency, OpenAI plan/model eligibility and representative quality review.
- Seller-authorized national/state registry access and reviewed campaign/recording/prerecorded policies.
- Permissioned real replay set, scorecard calibration and controlled cohort/outcome trial.
- Mac/Windows headset/NAT/Bluetooth/sleep/long-call certification and production VM capacity/recovery point targets. Linux is outside this preview distribution.
- Apple Developer ID/notarization, Windows Authenticode publisher signing and a configured public production host/DNS/TLS.

Unconfigured live workflows remain unavailable or fail closed; approved offline preparation and simulation stay useful. No external gate is checked merely because a mock returned HTTP 200.
