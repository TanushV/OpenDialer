# OpenDialer

A Tauri desktop calling workspace with two authority modes: a private local backend and an authenticated shared PostgreSQL backend. The app includes lead import and correction, seller-scoped call permissions, number pools and procurement, script preparation, live guidance, evidence-linked review, and controlled model experiments.

**Development status, October 18:** the [completion and simplification plan](docs/REMEDIATION_PLAN.md) is being implemented with tests first; unfinished work and evidence are recorded there and in [STATUS.md](docs/STATUS.md). Current scope uses existing numbers and observed health, contextual Cerebras help, local follow-up and rep-booked meeting outcomes. It permits no new subscriptions/rentals or CRM/calendar synchronization. Raw speech/private AI content has a five-minute lifetime. These changes are not yet packaged in the 0.2.1 release below; earlier release checks do not certify the current work.

**Current release: [0.2.1 developer preview](https://github.com/TanushV/OpenDialer/releases/tag/v0.2.1).** The Mac Apple Silicon and Windows x64 apps include their Python backend; users do not need Python, Node, Rust, Docker or API keys to try isolated simulation. Live calling needs a configured Telnyx account and reviewed permissions. Provider contracts and simulation are tested; live carrier behavior, model quality, hardware audio and trusted OS publisher signing are separate acceptance gates. See [validation](docs/VALIDATION.md).

## Install and try

Download the matching package from [Releases](https://github.com/TanushV/OpenDialer/releases). On Mac Apple Silicon (macOS 14 or later), move OpenDialer to Applications once. On Windows x64, run the NSIS setup executable. Open it and choose **Try a simulated workspace**. All sample people and provider responses are fictional. Nothing is called, purchased or sent to an inference provider. The simulation uses a fixed, visibly labeled clock so permission tests remain reproducible.

The preview is signed for updater integrity; a trusted Apple Developer ID and notarization are not configured. macOS may require the standard user approval for an app downloaded outside the App Store. Do not disable Gatekeeper or remove quarantine as an installation procedure. Windows x64 NSIS/MSI packages are built and exercised on a disposable Windows runner with bundled WebView2 and timezone data. Hardware certification and trusted OS publisher signing remain separate gates. Linux is not in this preview distribution.

Try: Go ready → Get next lead → Start call → Simulate human answer → add a fictional turn → Hang up → Save outcome → Review → edit and save notes. Leads, Numbers, Intelligence, and Settings expose the remaining workflows. No inference key is needed for approved fallback scripts and static coaching.

**Updates:** Settings → App updates → Check for updates → Install update; quit and reopen when prompted. Updates verify a pinned signing key and preserve app identity, database IDs, suppressions and human notes. Never uninstall or erase application data to update.

## Use your own local workspace

Choose **Set up on this computer**. The bundled authority binds only to a random loopback port with a per-launch nonce; no shared server or account is required. Your live and simulation stores are separate. Live authority keys and connected session credentials use the operating system credential store. The UI does not receive master provider keys after configuration.

Import CSV with `name,phone,company,domain,timezone,region,segment,jurisdiction`. Use an explicit IANA timezone. Valid formatting is not proof of ownership or calling permission. In Settings, configure Telnyx, approve the business identity, publish a reviewed campaign policy, import authorized registry data and set budgets. In Numbers, sync existing owned inventory, review capacity, or search fresh carrier quotes before approving a bounded purchase. Advance onboarding only with actual identity and callback evidence. Run preflight before going ready.

Optional inference supports OpenAI Responses, an explicit OpenAI-compatible LiteLLM endpoint, and eligible official per-user ChatGPT plan usage. Cerebras through a dedicated OpenRouter route powers configured live coaching; inference failure leaves calling controls and approved fallbacks available. The plan route uses public APIs and never pools consumer sessions. Read [provider setup](deploy/runbooks/providers.md).

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
uv run pytest --cov=app -q
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
