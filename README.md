# OpenDialer

A desktop calling workspace for small internal sales teams, with two authority modes: a private local backend on this computer (SQLite) and a shared server for a team (PostgreSQL). It covers lead import and correction, compliant next-lead selection, Telnyx calling with a live Cerebras helper, post-call rundowns and coaching, follow-up tasks, script preparation, statistical estimates and controlled experiments, number capacity planning, costs and work time.

This is the lean build, 0.5.0 ([plan](docs/LEAN_PLAN.md)): internal use, no hardening layers. It does not read 0.2/0.3 workspaces: the desktop starts a fresh one in its own data folders and leaves old data untouched, and a server refuses an old database. What is proven and what is not is in [validation](docs/VALIDATION.md). Simulated providers prove software behavior only; live carrier calls, funded inference quality, the DNC vendor account, hardware audio and campaign lift need their own evidence.

## Try it

Run the backend and the UI from source (see Develop), open the UI and choose **Try a simulated workspace**. This entry and the simulated call controls are development-only and are not offered in release builds. Sample people and provider responses are fictional; nothing is called, bought or sent to an inference provider. Try: Go ready → Get next lead → Start call → Simulate human answer → add a turn → Hang up → Save outcome → Review.

## Rep quickstart

1. Choose the workspace, campaign and audio device, then Go ready. Press **Space** or **Next call** to call the scheduler's next eligible lead. Get next lead previews the person and script first. The scheduler decides when another attempt is worthwhile, within campaign limits; there is no unconditional double dial.
2. Read the plain-text script. In **Intelligence**, upload or paste Markdown, preview its sections and save it. Headings can describe openings, questions, offers, objections and alternate wording. Script memory & variations holds the learning history and controlled tests. The helper above it follows stable turns and may stay quiet; F1 asks for private help. Mute, pin or dismiss tips and mark them useful, wrong or late.
3. Use Opt out the moment it is asked for (or the prospect presses 9). Voicemail is offered only on the final permitted attempt.
4. Confirmed carrier results and supported, reviewed AI outcomes save automatically; expand **Correct outcome** to amend them. Unknown results, bookings and callback times still need confirmation. Optionally enable the next scheduled call five seconds after voicemail ends; Cancel and Pause after call take precedence. A detected mailbox is not proof a message was delivered.
5. In Follow-ups, confirm, reschedule, snooze, assign, complete or cancel tasks. Pause when unavailable.

Practice starts from reviewed coaching opportunities and supported negative statistical findings. Number workload adapts to observed delivery within its reviewed daily maximum; staffing and current obligations constrain the capacity planner.

Research and post-call updates require named application tool calls. The live helper keeps its streaming display. AI-saved dispositions are labeled and excluded from observed training labels until human confirmation.

Raw speech, quotes and helper text expire within five minutes. There is no call recording.

## Local workspace

Choose **Set up on this computer**. The bundled authority listens only on a random loopback port with a per-launch token. Import a CSV with `name,phone,company,domain,timezone,region,segment,jurisdiction`. In Settings, enter provider keys (stored on this computer as plain JSON), approve the business identity, publish a campaign policy, import registry data and set budgets. In Numbers, sync your existing Telnyx numbers; buying numbers is blocked by policy. Run preflight before going ready. Signed Telnyx events need an HTTPS webhook route; see [deploy](deploy/runbooks/deploy.md) for the SSH forwarding recipe.

Research and script writing can use OpenAI, an OpenAI-compatible endpoint or a rep's own ChatGPT plan. Live help and rundowns use Cerebras through OpenRouter. See [providers](deploy/runbooks/providers.md).

## Team server

Deploy the same backend with PostgreSQL using [the deployment runbook](deploy/runbooks/deploy.md), bootstrap the first admin with the one-time token, create rep users, and choose **Connect to a server** in each desktop. Backups, restore and stuck-call recovery are in [recovery](deploy/runbooks/recovery.md).

## Develop

Requirements: Python 3.12 with uv, Node 24, Rust with the Tauri prerequisites.

```sh
uv sync --frozen --python 3.12
npm --prefix apps/desktop ci
uv run pytest services/backend/tests tests -q -n auto
uv run ruff check services/backend tests scripts deploy/backup
npm --prefix apps/desktop test
npm --prefix apps/desktop run test:e2e
uv run python scripts/bench.py
uv run python scripts/build_sidecar.py && cargo test --manifest-path apps/desktop/src-tauri/Cargo.toml
```

`services/backend/tests/acceptance/` drives every feature over HTTP. Set `OPENDIALER_TEST_POSTGRES_URL` to the disposable test database (`scripts/disposable_database.py` refuses anything else) to run the suites on PostgreSQL. Playwright starts its own disposable authorities and never touches installed app data. Releases: [release procedure](deploy/runbooks/releases.md).

Apache-2.0. Third-party notices are bundled with releases.
