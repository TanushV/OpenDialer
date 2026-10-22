# OpenDialer

A Tauri desktop calling workspace with two authority modes: a private local backend and an authenticated shared PostgreSQL backend. The app includes lead import and correction, seller-scoped call permissions, existing number pools and capacity proposals, script preparation, live guidance, evidence-linked review, and controlled model experiments.

**Current release: [0.3.3 developer preview](https://github.com/TanushV/OpenDialer/releases/tag/v0.3.3), October 21, 2026.** It replaces 0.3.0–0.3.2, each withdrawn the same day for a defect found while validating the update from 0.2.1. It is the lean completion of the [completion plan](docs/REMEDIATION_PLAN.md), with the binding amendments at the top of the [execution specification](docs/EXECUTION_SPEC.md#0-october-20-lean-completion-amendments). The Mac Apple Silicon and Windows x64 apps include their Python backend and a separate trainer; users do not need Python, Node, Rust, Docker or API keys to try isolated simulation. Live calling needs a configured Telnyx account and reviewed permissions.

Scope: existing numbers and observed health, a proactive Cerebras helper above the prepared script, one conversation state for live help, the rundown and the next call, local follow-up and rep-booked meeting outcomes. No new subscriptions or rentals, no call recording, no CRM/calendar synchronization. Raw speech and quote-bearing private AI content have a five-minute lifetime. Research and script writing can optionally run on the user's own ChatGPT plan.

What is proven, and what is not, is in [validation](docs/VALIDATION.md); execution evidence is in [STATUS.md](docs/STATUS.md). Provider contracts and simulation are tested. Live carrier calls, funded Cerebras quality and latency, the DNC vendor account, provider retention terms, hardware audio, measured campaign lift and trusted publisher signing are separate external gates. Connected (server) latency is not certified on the 2-vCPU reference.

## Install and try

Download the matching package from [Releases](https://github.com/TanushV/OpenDialer/releases). On Mac Apple Silicon (macOS 14 or later), move OpenDialer to Applications once. On Windows x64, run the NSIS setup executable. Open it and choose **Try a simulated workspace**. All sample people and provider responses are fictional. Nothing is called, purchased or sent to an inference provider. The simulation uses a fixed, visibly labeled clock so permission tests remain reproducible.

The preview is signed for updater integrity; a trusted Apple Developer ID and notarization are not configured. macOS may require the standard user approval for an app downloaded outside the App Store. Do not disable Gatekeeper or remove quarantine as an installation procedure. Windows x64 NSIS/MSI packages are built and exercised on a disposable Windows runner with bundled WebView2 and timezone data. Hardware certification and trusted OS publisher signing remain separate gates. Linux is not in this preview distribution.

Try: Go ready → Get next lead → Start call → Simulate human answer → add a fictional turn → Hang up → Save outcome → Review → edit and save notes. Leads, Numbers, Intelligence, and Settings expose the remaining workflows. No inference key is needed for approved fallback scripts and cached knowledge. Freeform Cerebras assistance requires its configured and permitted provider capability.

**Updates:** Settings → App updates → Check for updates → Install update; quit and reopen when prompted. Updates verify a pinned signing key and preserve app identity, database IDs, suppressions and human notes. Never uninstall or erase application data to update.

## Rep quickstart

1. Choose the correct local or connected workspace. Simulation is a separate store with fictional people. Restore your saved session; changing authority requires paused calling and reconciled pending work.
2. Choose the campaign and audio device. Go ready only when telephony is connected. Get next lead shows the prepared script, caller identity, prospect local time, previous promises and why this person is due.
3. Read the set script. The helper stays at the top while the script scrolls. It follows stable turns automatically; it can remain quiet. F1 opens private help. Mute tips, pin or dismiss guidance, or give useful/wrong/late feedback without interrupting call controls. Provider failures leave the script and hangup available.
4. Use Opt out immediately when requested. A hard denial blocks another outbound attempt independently of AI. Voicemail is available only on the final permitted attempt and only with separate permission and verified mailbox/topology evidence.
5. Hang up, select what happened and who you spoke with, add your note, and confirm any callback time. Save outcome immediately; analysis may still show pending/partial. Book meetings in your own workflow, then record their status here. Booked creates an outreach hold; time passing does not prove attendance.
6. In Follow-ups, confirm uncertain dates, assign, reschedule, snooze, cancel or complete the existing task. Snooze moves its reminder, not the promised callback time. Correct an outcome from Review. The next-call brief refreshes in the background while the active script stays fixed.
7. Pause when unavailable. Review and coaching retain concise observations, your notes and assessed rubric coverage. Raw speech and quote-bearing private help expire within five minutes; there is no call recording or transcript archive.

The numeric prospect view shows separate pickup, right-party and conversion estimates with their support. Sparse estimates use visible pooled priors; an unknown is not a zero score. Number warnings distinguish observed deterioration from an external spam label. Nothing in a synthetic run proves conversion lift.

## Use your own local workspace

Choose **Set up on this computer**. The bundled authority binds only to a random loopback port with a per-launch nonce; no shared server or account is required. Your live and simulation stores are separate. Live authority keys and connected session credentials use the operating system credential store. The UI does not receive master provider keys after configuration.

Import CSV with `name,phone,company,domain,timezone,region,segment,jurisdiction`. Use an explicit IANA timezone. Valid formatting is not proof of ownership or calling permission. In Settings, configure Telnyx, approve the business identity, publish a reviewed campaign policy, import authorized registry data and set budgets. In Numbers, sync existing owned inventory and compare its usable capacity with the list. New number rentals and paid reputation subscriptions are blocked by the accepted cost policy; quotes show a proposal, not purchase permission. Advance onboarding only with actual identity and callback evidence. Run preflight before going ready.

Optional research/script inference supports OpenAI Responses, an explicit OpenAI-compatible endpoint (LiteLLM, OpenRouter or similar) or the user's own ChatGPT plan (signed in per user; it never serves live speech). Cerebras through a dedicated OpenRouter route powers live coaching and the post-call rundown; inference failure leaves calling controls and approved fallbacks available. Read [provider setup](deploy/runbooks/providers.md).

A private machine still needs a reachable **HTTPS webhook-only route** for signed Telnyx events. Forward only `/webhooks/telnyx`; keep administrative routes private. There is no offline outbound bypass when authoritative permissions cannot be checked.

See [optimization and evaluation](docs/OPTIMIZATION.md) for number forecasting, frozen operational learning, controlled model pilots, conversion corrections and grounded live guidance.

## Connect a team

Deploy the same backend with PostgreSQL using [the deployment runbook](deploy/runbooks/deploy.md). Set independent persistent secrets outside Git, configure DNS/TLS, create the first admin using the one-time bootstrap token, and create scoped team users with individual incoming-only WebRTC credentials. On each desktop select **Connect to a server** and sign in. HTTPS is required except on loopback test servers.

Each call is authorized by the shared authority; reservations, seller isolation, suppressions, worker leases and stable caller IDs are enforced centrally. Desktop caches permit protected viewing during an outage; new calls pause and pending opt-outs must synchronize.

## Develop and verify

Requirements: Python 3.12, uv, Node 24, Rust and Tauri platform prerequisites. Locked dependencies are committed.

```sh
uv sync --frozen --python 3.12
npm --prefix apps/desktop ci
uv run pytest services/backend/tests tests -q -n auto   # add --cov=app --cov-branch for coverage
uv run ruff check services/backend/app services/backend/tests tests scripts deploy/backup
npm --prefix apps/desktop run build
npm --prefix apps/desktop test
npm --prefix apps/desktop run test:e2e
uv run python scripts/build_sidecar.py
cargo test --locked --manifest-path apps/desktop/src-tauri/Cargo.toml
npm --prefix apps/desktop run tauri build
```

Playwright starts disposable HTTP authorities and the development UI. Tests never use installed app data. For real PostgreSQL domain tests, set `OPENDIALER_TEST_POSTGRES_URL` to a **disposable test database**; the harness drops/recreates its schema. Do not point it at production or the installed app's store.

Release CI freezes a standalone sidecar on each platform and creates signed updater artifacts. See [release procedure](deploy/runbooks/releases.md), [architecture](docs/adr/0001-authority.md), [security and retention](docs/policies/security.md), [backup/recovery](deploy/runbooks/recovery.md), the [tracked implementation plan](docs/PLAN.md), and the [full original specification](docs/spec/IMPLEMENTATION_SPEC.md).

Apache-2.0. Third-party dependency notices are included with releases; no restricted neural model weights are bundled.
