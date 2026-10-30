# Set up your first workspace

[Install OpenDialer 0.6.0](INSTALL.md) first, or update from **Settings → App updates**.

## Choose a workspace

- **Working alone:** choose **Start a local workspace**. Your data and provider settings stay on this computer. No separate Python or database installation is needed.
- **Joining a team:** choose **Connect to a server**, enter the HTTPS address from your admin and sign in. Your admin handles providers and the shared database.

For local calling with the shared webhook, use the shared setup below. Existing direct-local builds still use
[webhook forwarding](../deploy/runbooks/deploy.md#local-authority-with-an-existing-ssh-host); hosted mode is unchanged.
A sleeping or offline laptop cannot handle callbacks; configure and test the carrier fallback.

## Local calling with the shared webhook

Available in 0.6.0. Import once; the connection restores and refreshes automatically.

1. In a local workspace, open **Settings → Setup → Calling → Import calling setup (.json)**.
   Choose the private file prepared for this computer. It imports the Telnyx application, verification key,
   browser credential, SIP username and shared webhook URL. Included provider keys are saved too.
2. Wait for **Connected**. Login refresh and reconnect happen automatically. There is no tunnel token,
   SIP password to enter, Supabase dashboard login or terminal command for callers.
3. Save a Telnyx key if omitted. Add your OpenRouter key in **AI assistance** and import your own Codex
   login there if using the ChatGPT route. DNC coverage and business/campaign settings remain separate.
4. In **Numbers**, sync numbers already assigned to your calling application, or explicitly approve a priced
   purchase within your limits. Configure the number, complete identity evidence, and call it from another phone
   to verify callback delivery. A signed incoming event assigns that number to this workspace at the relay;
   normal callback evidence and outbound-readiness checks still apply. Nothing buys or warms up numbers automatically.
5. Import leads, upload your Markdown script in **Intelligence**, run preflight, check your headset and go ready.

Each independent computer needs its own setup file and Telnyx application/agent credential. All use the **same
webhook URL**. Do not copy one live workspace profile to two computers: the receiver permits one store/consumer.
Keep the file private. Restoring or moving an existing store needs its connection/session files too, or an operator
must explicitly release the old binding after confirming that computer has stopped.

Keep the app open and computer awake. New calls pause if delivery or transcription fails; hangup remains usable.
Resolve outstanding calls in Recovery, then use **Reconnect** in Calling. Reconnect after a delivery gap acknowledges
that old events may be missing; it does not recreate lost conversations. Pending controls expire after 24 hours,
with a visible gap, and acknowledged payloads are immediately removed (dedupe IDs remain seven days).

Live speech uses continuous Telnyx Soniox streams, one per speaker. The automatic helper groups stable text into
fixed two-second windows. Supabase carries control events only, not audio, transcripts, leads, scripts or history.
Carrier/STT/AI usage remains billable; the shared service uses the existing Free Supabase plan without an uptime SLA.

## Connect services and prepare to call

Open **Settings → Setup** and complete the checklist in order.


1. **Calling (Telnyx):** use the setup-file import above when provided. For direct/hosted configuration, paste the Telnyx API v2 key, Call Control connection ID, webhook public key and public HTTPS webhook URL. Add the agent credential and carrier approval details. **Save Telnyx settings** saves the key and connection fields together. Local calling needs a reachable webhook forward; check the saved ingress.
2. **AI assistance:** add an OpenRouter key for the Cerebras live helper and rundowns. For research/script writing with your Codex ChatGPT login, select **ChatGPT via Codex · import login**, then import the file as described below. An OpenAI key, compatible endpoint or the separate eligible ChatGPT authorization route is an alternative, not an additional requirement.
3. Complete business identity, campaign hours, authorized DNC coverage and budgets. A DNC API key is needed only for the configured prepaid lookup route; importing authorized registry data is a separate supported path.
4. In **Numbers**, sync owned numbers. To find additions, select your lead list and click **Plan state coverage**, then **Find numbers in [state]**. Review the metro suggestions and exact rental quotes under **Add numbers with approval**. Save count and spending limits before approving a purchase; zero blocks purchases. Complete routing and a genuine inbound callback test, then approve limited legitimate use. Buying or syncing does not start warmup.
5. Upload your Markdown script in **Intelligence**, import leads, run preflight and check your audio before going ready. Allocation and next-lead selection use the eligible pool automatically; capacity planning does not place orders.

Keys are saved to the authority, not sent through chat. See [provider onboarding](../deploy/runbooks/providers.md) for account configuration, callback limitations and live verification.

### Numbers for a lead list

Name your list when importing a CSV (the filename is suggested). Include `state` as a US abbreviation or full name,
for example `CA` or `California`, and a separate IANA `timezone`. Existing imports appear as **Unnamed imports**.
Duplicate phones in the same campaign retain their original list and caller identity.

**Numbers → Plan numbers for list → Plan state coverage** shows the selected list's demand, usable owned capacity,
numbers awaiting setup, and proposed additions by state. Choose **All campaign leads** to check the combined demand;
the same owned pool serves multiple lists, so separate list previews are not reservations and must not be added together.
Staffing, calling windows, screening and pending setup still constrain the plan. A new number cannot fix those limits.

The scheduler keeps a lead's existing caller identity. For an unbound lead, it prefers ready numbers in the same state,
balancing current load, daily usage and observed health; an exact area code breaks otherwise equal choices.
It does not require a separate number for every area code. Eligible general-pool numbers remain a fallback.
State inference from a phone's area code is labeled as an estimate and never supplies a timezone or legal jurisdiction.

**Find numbers in [state] → Find available numbers** searches up to three suggested in-state area codes and shows
current prices. Familiar metro defaults are ordered using matching area codes in your list. You can also enter a
specific area code. These are preferences, not population rankings or evidence of better pickup rates. The offline
state mapping comes from [NANPA's area-code database](https://nanpa.com/reports/npa-reports); actual availability and
prices come from Telnyx. Each exact rental still needs approval within your number-count and spending limits.

New number-allocation comparisons group leads by state. An older area-code comparison retains its history but
does not assign new calls; stop it and start a new comparison if you want to evaluate the new allocation policy.


## Connect your Codex login

1. On a trusted computer with [Codex installed](https://learn.chatgpt.com/docs/auth), run:

   ```sh
   codex -c cli_auth_credentials_store='"file"' login
   ```

   Choose ChatGPT and complete sign-in. The file-store flag makes a transferable file instead of using the OS keychain.
2. Find **`auth.json`** in your home folder's **`.codex`** directory: `~/.codex/auth.json` on Mac/Linux,
   or `%USERPROFILE%\.codex\auth.json` on Windows. A custom `CODEX_HOME` changes this location.
   On a Mac file picker, press **Command–Shift–G** to enter the folder; on Windows paste it into the address bar.
3. In OpenDialer, open **Settings → Setup → AI assistance**, choose **ChatGPT via Codex · import login**,
   and select the file. A successful import displays the account and saves this route. Reps on a team server
   have **Your Codex login** directly in Settings; the admin selects the workspace's inference route.

OpenDialer keeps a separate per-user copy on its authority computer and bundles the Codex runtime.
Your friend does not need to install Codex separately to use this build. On a hosted workspace, the file goes
to the team server, so only import into a server you trust. Keep the file private and remove temporary transfer copies.

The official runtime refreshes expiring sessions during use and OpenDialer retains the refreshed credentials
across restarts. A revoked login, expired refresh token or account restriction still requires a fresh sign-in/import;
this is not a permanent token. Prefer a dedicated login for this installation rather than concurrently using copies
of the same refresh token on several machines. **Disconnect Codex** deletes OpenDialer's copy without logging out
your other Codex installation or revoking the account globally.

This route uses your plan's limits for preparation and analysis. It does not enable paid API search, change
live coaching, or bypass the separate post-call privacy gate. AI results must use named submission tools and pass
the existing application validation. A successful login check does not prove model access or output quality.

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

## Saving credentials

Paste provider keys in Settings and save. Import Codex login and the per-PC webhook setup file once.
Saved settings, personal logins and webhook sessions are encrypted; the desktop keeps the encryption key
in macOS Keychain or Windows Credential Manager. Existing saved credentials convert on first launch.
If the OS asks for credential-store access, verify it is OpenDialer. No separate encryption key is needed
for desktop setup. Keep imported setup/login files private: they still contain credentials.

Telnyx/OpenRouter API keys, Codex tokens and webhook login/session credentials are secret. Telnyx application
and credential IDs, SIP username, webhook verification **public** key, webhook URL and Supabase publishable
key are identifiers/public configuration, not passwords. No separate SIP password or Cloudflare token is needed
for the managed-local flow. See [security and recovery](policies/security.md).
