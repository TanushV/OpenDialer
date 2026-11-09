# Setup guide

From install to your first call. Each step names the page and the button.

## 1. Install and open

Download the Mac `.dmg` or Windows `.exe` from the [latest release](https://github.com/TanushV/OpenDialer/releases/latest)
and open OpenDialer. Choose **Start a local workspace**. Your data stays on this computer.

## 2. Settings

Settings shows one row per thing to set, and a status at the top: **Ready to call**, or the first thing to fix.

1. **Telnyx API key.** In the Telnyx portal open API Keys, copy one, paste it and click **Save and check**.
2. **Call connection.** Import the calling setup file you were given. It reconnects by itself after that; if it ever
   cannot, the Dialer shows **Reconnect**.
3. **ChatGPT login.** In Terminal run `codex login`, sign in, then click **Use the ChatGPT login on this computer** in
   the ChatGPT row. Research and scripts use your ChatGPT plan.
4. **OpenRouter key.** Paste it and click **Save and check**. It powers live tips and the after-call summary.
5. **Calling hours** are edited in their row (8 AM to 5 PM in each lead's time zone by default).
6. **Test phone.** Type your own number and click **Save and check**, then **Test call**: the Dialer dials your phone
   right away, at any hour. Save that call's outcome and normal calling returns.
7. **Optional, for HubSpot:** the **HubSpot Service Key** (Settings → Integrations → Service Keys in HubSpot, with
   contact, list, owner and call scopes), then **Calls for** (the HubSpot user whose contacts this computer calls), the
   **Treg token** for number checks and the **RealValidito key and secret** for do-not-call checks.

## 3. Numbers

Open **Numbers** and buy a local number or add one you own. A new number sets itself up and starts at 15 real calls a day,
rising to 60 as you use it. The first time, call one of your numbers from your phone and answer it in the app; that
proves the route, and every number opens.

## 4. Script

Open **Script**, paste your Markdown script (start from the [example](examples/script.md)) and click **Save script**.
The page says **Saved** or **Unsaved changes**; calls use the saved version. Required sections: `# Opening`,
`# Screening`, `# Discovery`, `# Close`. Each bullet is an option the agent may pick; `{{ ... }}` is a line the agent
writes for each lead; `{first_name}`, `{company}`, `{rep}` and `{seller}` are filled in. **Add trigger list** adds the
"why now" rules research looks for.

## 5. Leads

On **Leads**, either name a list and import a CSV with at least `name` and `phone` (the app shows how it read the
columns before saving), or **Sync a HubSpot list** with a spend limit for number checks. If HubSpot has no lists yet, make
one in HubSpot under Contacts → Lists. Each lead is researched and scripted; the research panel at the bottom shows
progress.

## 6. Call

On **Dialer**, the paused screen says how many leads can be called now, or why none can (for example, everyone is outside
calling hours). Press **Start calling** or Space.

| Key | When | Does |
| --- | --- | --- |
| Space | Paused | Start calling |
| Space | Dialing or a mailbox | Skip this call |
| S | Voicemail is being skipped | Stay on the line |
| P | Any time | Pause after this call |
| Esc | Talking | Hang up |
| 1 to 6 | Call ended | Meeting booked, Callback, Not interested, Gatekeeper, Wrong number, Voicemail |
| Enter | Call ended with a suggestion | Accept the suggested outcome |

After you save, the next call starts in 3 seconds. The wrap-up shows the call's summary and next step.

## 7. Review

**Review** opens with **Needs you**: each issue in a line, with the button that fixes it. Below are follow-ups, past
calls (with transcripts kept 30 days, deletable) and **What it has learned**.

## When something is blocked

Every block names its reason. If it says it clears by itself, wait; if it does not within the stated time, the app
repairs it, and if that fails it shows **Reconnect**. Anything else names the page and the control that fixes it.
