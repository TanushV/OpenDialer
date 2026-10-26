# Set up your first workspace

[Install the app](INSTALL.md) first. These instructions describe current `main`.
The simplified setup and approved number-order flow are **not yet in the published 0.5.1 installer**.

## Choose a workspace

- **Working alone:** choose **Start a local workspace**. Your data and provider settings stay on this computer. No separate Python or database installation is needed.
- **Joining a team:** choose **Connect to a server**, enter the HTTPS address from your admin and sign in. Your admin handles providers and the shared database.

Local calling needs a public HTTPS route for Telnyx webhooks. Use an existing SSH host with the
[forwarding instructions](../deploy/runbooks/deploy.md#local-authority-with-an-existing-ssh-host), or have an admin deploy an always-on team server.
A sleeping or offline laptop cannot handle callbacks; configure and test the carrier fallback.

## Connect services and prepare to call

Open **Settings → Setup** and complete the checklist in order.


1. **Calling (Telnyx):** paste the Telnyx API v2 key, Call Control connection ID, webhook public key and public HTTPS webhook URL. Add the agent credential and carrier approval details. **Save Telnyx settings** saves the key and connection fields together. Local calling needs a reachable webhook forward; check the saved ingress.
2. **AI assistance:** add an OpenRouter key for the Cerebras live helper and rundowns. For research/script writing, select **Official eligible ChatGPT plan usage → Continue with ChatGPT**; that route does not need an OpenAI API key. An OpenAI key or compatible endpoint is an alternative, not an additional requirement.
3. Complete business identity, campaign hours, authorized DNC coverage and budgets. A DNC API key is needed only for the configured prepaid lookup route; importing authorized registry data is a separate supported path.
4. In **Numbers**, sync owned numbers, or open **Add numbers with approval**, save count and spending limits, search an area code, review a quote and explicitly approve its recurring rental. Zero limits block new purchases. Complete routing and a genuine inbound callback test, then approve limited legitimate use. Buying or syncing does not start warmup.
5. Upload your Markdown script in **Intelligence**, import leads, run preflight and check your audio before going ready. Allocation and next-lead selection use the eligible pool automatically; capacity planning does not place orders.

Keys are saved to the authority, not sent through chat. See [provider onboarding](../deploy/runbooks/providers.md) for account configuration, callback limitations and live verification.


## Make a call


1. Choose the workspace, campaign and audio device, then Go ready. Press **Space** or **Next call** to call the scheduler's next eligible lead. Get next lead previews the person and script first. The scheduler decides when another attempt is worthwhile, within campaign limits; there is no unconditional double dial.
2. Read the plain-text script. In **Intelligence**, upload or paste Markdown, preview its sections and save it. Headings can describe openings, questions, offers, objections and alternate wording. Script memory & variations holds the learning history and controlled tests. The helper above it follows stable turns and may stay quiet; F1 asks for private help. Mute, pin or dismiss tips and mark them useful, wrong or late.
3. Use Opt out the moment it is asked for (or the prospect presses 9). Voicemail detection remains available; prerecorded voicemail drop is removed.
4. Confirmed carrier results and supported, reviewed AI outcomes save automatically; expand **Correct outcome** to amend them. Unknown results, bookings and callback times still need confirmation. Optionally enable the next scheduled call five seconds after voicemail ends; Cancel and Pause after call take precedence. Voicemail drop is not available; a detected mailbox is not proof a message was delivered. New workspaces allow calls from 8 a.m. until 9 p.m. in the prospect’s timezone; existing campaign hours are editable in Settings.
5. In Follow-ups, confirm, reschedule, snooze, assign, complete or cancel tasks. Pause when unavailable.

Practice starts from reviewed coaching opportunities and supported negative statistical findings. Number workload adapts to observed delivery within its reviewed daily maximum; staffing and current obligations constrain the capacity planner.

Research and post-call updates require named application tool calls. The live helper keeps its streaming display. AI-saved dispositions are labeled and excluded from observed training labels until human confirmation.

Raw speech, quotes and helper text expire within five minutes. There is no call recording.


## If something is blocked

| Problem | Next step |
| --- | --- |
| No eligible next lead | Run preflight; check campaign hours, lead timezone, DNC evidence, budgets, your ready state and number readiness. |
| Number exists but cannot call | Complete identity/routing, a real inbound callback and limited-use approval in Numbers. Syncing alone is not readiness. |
| Cannot receive webhooks | Check the public HTTPS URL, verification key and saved ingress. Keep the local computer and tunnel online. |
| ChatGPT is missing or setup looks different | Check the version and the release/source notice above. Current source moves ChatGPT into AI assistance. |
| Provider fails after saving a key | Check account eligibility, balance and connection settings using the [provider guide](../deploy/runbooks/providers.md). A saved key alone does not prove service access. |
| Microphone unavailable | Allow microphone access in the operating system, select the intended input/output and check the headset. |

For backups or interrupted calls, see [recovery](../deploy/runbooks/recovery.md).
