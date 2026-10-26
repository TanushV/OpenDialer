# OpenDialer

A desktop calling workspace for small internal sales teams, with two authority modes: a private local backend on this computer (SQLite) and a shared server for a team (PostgreSQL). It covers lead import and correction, compliant next-lead selection, Telnyx calling with a live Cerebras helper, post-call rundowns and coaching, follow-up tasks, script preparation, statistical estimates and controlled experiments, number capacity planning, costs and work time.

This is the lean build, 0.5.1 ([plan](docs/LEAN_PLAN.md)): internal use, no hardening layers. It does not read 0.2/0.3 workspaces: the desktop starts a fresh one in its own data folders and leaves old data untouched, and a server refuses an old database. What is proven and what is not is in [validation](docs/VALIDATION.md). Simulated providers prove software behavior only; live carrier calls, funded inference quality, the DNC vendor account, hardware audio and campaign lift need their own evidence.

## Try it

Run the backend and the UI from source (see Develop), open the UI and choose **Try a simulated workspace**. This entry and the simulated call controls are development-only and are not offered in release builds. Sample people and provider responses are fictional; nothing is called, bought or sent to an inference provider. Try: Go ready → Get next lead → Start call → Simulate human answer → add a turn → Hang up → Save outcome → Review.

## Rep quickstart

1. Choose the workspace, campaign and audio device, then Go ready. Press **Space** or **Next call** to call the scheduler's next eligible lead. Get next lead previews the person and script first. The scheduler decides when another attempt is worthwhile, within campaign limits; there is no unconditional double dial.
2. Read the plain-text script. In **Intelligence**, upload or paste Markdown, preview its sections and save it. Headings can describe openings, questions, offers, objections and alternate wording. Script memory & variations holds the learning history and controlled tests. The helper above it follows stable turns and may stay quiet; F1 asks for private help. Mute, pin or dismiss tips and mark them useful, wrong or late.
3. Use Opt out the moment it is asked for (or the prospect presses 9). Voicemail detection remains available; prerecorded voicemail drop is removed.
4. Confirmed carrier results and supported, reviewed AI outcomes save automatically; expand **Correct outcome** to amend them. Unknown results, bookings and callback times still need confirmation. Optionally enable the next scheduled call five seconds after voicemail ends; Cancel and Pause after call take precedence. Voicemail drop is not available; a detected mailbox is not proof a message was delivered. New workspaces allow calls from 8 a.m. until 9 p.m. in the prospect’s timezone; existing campaign hours are editable in Settings.
5. In Follow-ups, confirm, reschedule, snooze, assign, complete or cancel tasks. Pause when unavailable.

Practice starts from reviewed coaching opportunities and supported negative statistical findings. Number workload adapts to observed delivery within its reviewed daily maximum; staffing and current obligations constrain the capacity planner.

Research and post-call updates require named application tool calls. The live helper keeps its streaming display. AI-saved dispositions are labeled and excluded from observed training labels until human confirmation.

Raw speech, quotes and helper text expire within five minutes. There is no call recording.

## Local workspace

Choose **Start a local workspace**. The bundled authority listens only on a random loopback port with a per-launch token. Import a CSV with `name,phone,company,domain,timezone,region,segment,jurisdiction`. In Settings, enter provider keys (stored on this computer as plain JSON), approve the business identity, publish a campaign policy, import registry data and set budgets. In Numbers, sync existing Telnyx numbers or set count/cost limits, find a current quote and approve a rental. Allocation uses ready inventory automatically; purchasing does not skip routing or callback setup. Run preflight before going ready. Signed Telnyx events need an HTTPS webhook route; see [deploy](deploy/runbooks/deploy.md) for the SSH forwarding recipe.

The setup path is **Settings → Setup**:

1. **Calling (Telnyx):** paste the Telnyx API v2 key, Call Control connection ID, webhook public key and public HTTPS webhook URL. Add the agent credential and carrier approval details. **Save Telnyx settings** saves the key and connection fields together. Local calling needs a reachable webhook forward; check the saved ingress.
2. **AI assistance:** add an OpenRouter key for the Cerebras live helper and rundowns. For research/script writing, select **Official eligible ChatGPT plan usage → Continue with ChatGPT**; that route does not need an OpenAI API key. An OpenAI key or compatible endpoint is an alternative, not an additional requirement.
3. Complete business identity, campaign hours, authorized DNC coverage and budgets. A DNC API key is needed only for the configured prepaid lookup route; importing authorized registry data is a separate supported path.
4. In **Numbers**, sync owned numbers, or open **Add numbers with approval**, save count and spending limits, search an area code, review a quote and explicitly approve its recurring rental. Zero limits block new purchases. Complete routing and a genuine inbound callback test, then approve limited legitimate use. Buying or syncing does not start warmup.
5. Upload your Markdown script in **Intelligence**, import leads, run preflight and check your audio before going ready. Allocation and next-lead selection use the eligible pool automatically; capacity planning does not place orders.

Keys are saved to the authority, not sent through chat. See [provider onboarding](deploy/runbooks/providers.md) for account configuration, callback limitations and live verification.

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
