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

Cargo.lock was also checked against OSV. Two transitive Linux-only packages have advisories; neither is active in the Mac ARM64 or Windows x64 dependency trees. Linux packages are excluded from this preview. This is not a claim that the entire lockfile is advisory-free.

Native local workflow already exercised through computer use: installed bundle opens the sample workspace; readiness, reservation, call start, simulated answer/transcript, coach fallback, hangup, callback disposition, review and human-note edit/save work. A second sample lead was suppressed before the update. Update verification compares exact lead/call/note/suppression IDs, human-note hashes and suppression counts and checks encrypted payloads. The completed update/connected receipts are appended below when observed.

## Remaining external acceptance gates

- Actual carrier traffic approval, per-rep direct-call bypass attempt, caller-ID/bridge/AMD/voicemail topology, callback audio and carrier/invoice billing receipts.
- Live Soniox/Parakeet timing/attribution, Groq/model quotas and end-to-end STT-to-card latency, OpenAI plan/model eligibility and representative quality review.
- Seller-authorized national/state registry access and reviewed campaign/recording/prerecorded policies.
- Permissioned real replay set, scorecard calibration and controlled cohort/outcome trial.
- Windows/Linux headset/NAT/Bluetooth/sleep/long-call certification and production VM capacity/recovery point targets.
- Apple Developer ID/notarization, other platform signing and a configured public production host/DNS/TLS.

Unconfigured live workflows remain unavailable or fail closed; approved offline preparation and simulation stay useful. No external gate is checked merely because a mock returned HTTP 200.
