# Develop and test

[Documentation index](README.md) · [Contributor working agreement](../AGENTS.md)

## Prepare

Use Python 3.12 with uv, Node 24, and Rust stable plus the
[Tauri platform prerequisites](https://v2.tauri.app/start/prerequisites/) for native builds.
Run commands from the repository root unless shown otherwise.

```sh
uv sync --frozen --python 3.12
npm --prefix apps/desktop ci
(cd apps/desktop && npx playwright install chromium)
```

## Try the UI without real calls

In one terminal:

```sh
uv run python scripts/e2e_server.py local
```

In another:

```sh
cd apps/desktop
VITE_BACKEND_URL=http://127.0.0.1:8765 VITE_LOCAL_TOKEN=e2e-local-token npm run dev
```

These terminal examples use a POSIX shell (macOS/Linux). Open `http://127.0.0.1:1420` and choose
**Try a simulated workspace**. The temporary data disappears when the server stops.
Simulation controls are development-only and hidden in release builds. No calls or paid inference are made.
Do not launch a locally built app against installed user data.

## Run checks

```sh
uv run ruff check services/backend tests scripts deploy/backup
uv run ruff format --check services/backend tests scripts deploy/backup
uv run pytest services/backend/tests tests -q -n auto
npm --prefix apps/desktop run typecheck
npm --prefix apps/desktop run format:check
npm --prefix apps/desktop run build
npm --prefix apps/desktop test
uv run python scripts/build_sidecar.py
cargo test --manifest-path apps/desktop/src-tauri/Cargo.toml
```

For PostgreSQL and browser tests, first configure a **disposable** database using the
[handoff commands](HANDOFF.md). Set `OPENDIALER_TEST_POSTGRES_URL` and run the backend suite again.
Then run `npm --prefix apps/desktop run test:e2e`. Playwright starts disposable local and hosted authorities;
do not run two browser suites at once. Limit test workers when the computer is busy.

The [CI workflow](../.github/workflows/ci.yml) is the authoritative check list.
[Release procedure](../deploy/runbooks/releases.md) covers packaging and signing.
[Resource measurements](REQUIREMENTS.md) explain how to run the local benchmark.
