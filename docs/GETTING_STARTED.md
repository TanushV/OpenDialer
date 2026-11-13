# Setup guide

From install to your first call. Each step names the page and the button.

## 1. Install and open

Download the Mac `.dmg`, Windows `.exe`, or Linux `_amd64.deb` from the [latest release](https://github.com/TanushV/OpenDialer/releases/latest).
The Linux package targets Ubuntu 24.04 LTS on 64-bit Intel or AMD. From the download folder, run
`sudo apt install ./OpenDialer_2.1.4_amd64.deb` using the filename you downloaded, then open **OpenDialer** from the app menu.
On any platform, choose **Start a local workspace**. Your data stays on this computer.

## 2. Settings

Settings shows one row per thing to set, and a status at the top: **Ready to call**, or the first thing to fix.

1. **Telnyx API key.** In the Telnyx portal open API Keys, copy one, paste it and click **Save and check**.
2. **Call connection.** Click **Open calling setup (.json)** and choose `<rep>-calling-setup.json` from your credentials ZIP.
   The app reconnects by itself after that. If it needs help, the Dialer shows **Reconnect**.
3. **ChatGPT login.** In Terminal run `codex login` and sign into your own ChatGPT account. In Settings choose
   **Use the ChatGPT login on this computer**, or use **Open Codex auth.json** to import your own Codex login file.
   OpenRouter can run research and scripts if ChatGPT is unavailable.
4. **OpenRouter key.** Paste it and click **Save and check**. It powers live tips and the after-call summary.
5. **Calling hours** are edited in their row (8 AM to 5 PM in each lead's time zone by default).
6. **Test phone.** Type your own number and click **Save and check**, then **Test call**: the Dialer dials your phone
   right away, at any hour. Save that call's outcome and normal calling returns.
7. **HubSpot.** Shared profiles each have an assigned HubSpot segment. The setup can supply the **HubSpot Service Key**
   and the **Treg token**, which is used to look up numbers on contacts without a usable saved phone. Add the
   **RealValidito key and secret** in Settings to screen HubSpot contacts before calling.

## 3. Numbers

Shared caller IDs appear on **Numbers** after your calling profile connects. Each rep uses the existing pool with a
limit of up to 90 outbound attempts per number per rolling 24 hours. Incoming callbacks use no outbound capacity.
Standalone workspaces can add owned numbers under **Numbers** and test their routes there.

## 4. Script

Open **Script**. Use **Open calling script (.md)** or paste your Markdown script (start from the [example](examples/script.md)).
Click **Save script**.
The page says **Saved** or **Unsaved changes**; calls use the saved version. Required sections: `# Opening`,
`# Screening`, `# Discovery`, `# Close`. Each bullet is an option the agent may pick; `{{ ... }}` is a line the agent
writes for each lead; `{first_name}`, `{company}`, `{rep}` and `{seller}` are filled in. **Add trigger list** adds the
"why now" rules research looks for.

## 5. Leads

On **Leads**, either name a list and import a CSV with at least `name` and `phone` (the app shows how it read the
columns before saving), or **Sync a HubSpot list** with a spend limit for number checks. If HubSpot has no lists yet, make
one in HubSpot under Contacts → Lists. In **Research and scripts**, click **Generate scripts** for the list you want
personalized. Importing a list or saving the base script leaves generation idle. Callable leads can use the base script.

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
