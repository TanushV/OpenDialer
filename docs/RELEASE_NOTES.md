# OpenDialer 2.1.6

- **Run a multi-number test list.** Save a CSV in Settings, optionally generate personalized scripts for it in Leads, then run it in the Dialer. The Dialer works through each test phone once per run at any hour and pauses when the list is finished.
- **See why calling is waiting.** The main Dialer status shows the queue's reason and the next step for issues such as calling hours, screening and caller-ID availability.

Updating from 1.0.x through 2.1.5 keeps your workspace. Updating from 0.9.x or 0.10.x keeps your workspace and saved credentials. Versions 0.2 and 0.3 start a fresh workspace and old data stays untouched. macOS may ask for microphone access again. Updating from 0.6.0 or 0.6.1 may require you to re-enter keys and re-import setup or login files.

# OpenDialer 2.1.5

- **Check GitHub for signed updates.** Settings shows the installed desktop version, the result of the update check, and a link to the latest downloads for Mac, Windows and Ubuntu.
- **Get a suggestion after the prospect finishes speaking.** The live helper waits for a finalized prospect turn and a brief pause. It uses the call so far and the last script section delivered to pick a line from the prepared script.
- **Test your phone at any hour.** Test Call uses your saved test phone, even if its HubSpot record is stale or its outreach is complete. The Dialer shows the test destination and stops after that call. Leads omits the internal Call by date.

Updating from 1.0.x through 2.1.4 keeps your workspace. Updating from 0.9.x or 0.10.x keeps your workspace and saved credentials. Versions 0.2 and 0.3 start a fresh workspace and old data stays untouched. macOS may ask for microphone access again. Updating from 0.6.0 or 0.6.1 may require you to re-enter keys and re-import setup or login files.

# OpenDialer 2.1.4

- **Install on Ubuntu.** The `.deb` targets Ubuntu 24.04 LTS on 64-bit Intel and AMD.
- **Open the right setup file.** File buttons name the calling profile, Codex login, Markdown script and lead CSV they accept.
- **See shared caller IDs after connecting.** The Numbers page refreshes when your rep profile receives the existing pool.
- **Generate scripts per list.** Import contacts or save the base script, then select **Generate scripts** on a list in Leads when you want personalized scripts.
- **Keep saved HubSpot numbers.** Treg looks up a phone when the contact has no usable saved number. HubSpot contacts go through do-not-call screening before dialing.

Updating from 1.0.x through 2.1.3 keeps your workspace. Updating from 0.9.x or 0.10.x keeps your workspace and saved credentials. Versions 0.2 and 0.3 start a fresh workspace and old data stays untouched. macOS may ask for microphone access again. Updating from 0.6.0 or 0.6.1 may require you to re-enter keys and re-import setup or login files.

# OpenDialer 2.1.3

- **Install on Linux.** The x86-64 Debian package targets Ubuntu 24.04 LTS and uses the same calling profile setup as Mac and Windows.
- **Use saved HubSpot numbers.** OpenDialer uses Treg to find a number when the contact has no usable saved number. It runs do-not-call screening on HubSpot numbers before calling.

Updating from 2.0.0 through 2.1.2 keeps your workspace. Updating from 0.9.x or 0.10.x keeps your workspace and saved credentials. Versions 0.2 and 0.3 start a fresh workspace and old data stays untouched. macOS may ask for microphone access again. Updating from 0.6.0 or 0.6.1 may require you to re-enter keys and re-import setup or login files.

# OpenDialer 2.1.2

- **Open your saved workspace automatically.** OpenDialer uses the workspace on this computer and reconnects if the local service restarts. Entering a rep name for scripts is optional.
- **Includes the 2.1 desktop changes.** The update includes the layout fixes, shared caller IDs and callbacks routed to the rep who called. Each rep still imports their calling profile once on their computer.

Updating from 2.0.0 through 2.1.1 keeps your workspace. Updating from 0.9.x or 0.10.x keeps your workspace and saved credentials. Versions 0.2 and 0.3 start a fresh workspace and old data stays untouched. macOS may ask for microphone access again. Updating from 0.6.0 or 0.6.1 may require you to re-enter keys and re-import setup or login files.

# OpenDialer 2.1.1

- **Cleaner desktop layout.** Controls line up across Dialer, Leads, Review, Numbers, Script and Settings. Long names wrap, wide tables scroll within their panels, and call actions stay visible while you work.
- **Clearer number limits.** Numbers explains how new standalone numbers ramp up and how the shared pool counts outbound attempts. Incoming callbacks do not count against the shared outbound limit.

Updating from 2.0.0 or 2.1.0 keeps your workspace. Updating from 0.9.x or 0.10.x keeps your workspace and saved credentials. Versions 0.2 and 0.3 start a fresh workspace and old data stays untouched. macOS may ask for microphone access again. Updating from 0.6.0 or 0.6.1 may require you to re-enter keys and re-import setup or login files.

# OpenDialer 2.1.0

- **A shared number pool for reps.** One Telnyx application serves the team. Each computer imports its own calling profile and syncs one HubSpot segment. The pool remembers each prospect's caller ID across attempts and machines.
- **Callbacks open the prospect.** A return call rings the computer that placed the last confirmed call from that number. The matched prospect and last outcome appear on the incoming call. On a pool with hosted fallback enabled, an offline rep's callers hear the greeting and the rep sees a missed-call task after reconnecting.
- **Up to 90 outbound attempts per number in 24 hours.** The shared pool applies the limit across reps. Incoming callbacks stay eligible at the outbound limit.
- **Test mode picks the saved phone.** If that phone is already in HubSpot, Test call opens the HubSpot prospect even if a CSV copy exists. Prospect screening and retry dates still apply to regular HubSpot calls.

Updating from 2.0.0 keeps the local workspace. The relay operator enables shared calling for each participating computer. Updating from 0.9.x or 0.10.x keeps your workspace and saved credentials. Versions 0.2 and 0.3 start a fresh workspace and old data stays untouched. macOS may ask for microphone access again. Updating from 0.6.0 or 0.6.1 may require you to re-enter keys and re-import setup or login files.

# OpenDialer 2.0.0

- **Callbacks ring in the app.** While OpenDialer is open, on any page and paused or not, a call back to one of your numbers rings with a tone, bounces the Dock and shows who it is, with Answer and Decline. When the app is closed or nobody answers, the caller hears your greeting and you get a follow-up. Forwarding to your phone comes later.
- **A plain calling order.** Callbacks due first, then people never called, then whoever has waited longest, inside calling hours. StatML is archived (tagged `statml-archive`); Review now shows where answered calls end in your script instead.
- **Numbers finish setting up on their own.** A number still waiting on its order is re-checked with Telnyx every ten minutes until it is ready, so none stays "purchased".
- **Call screeners.** When a phone's own screener asks who is calling, the helper says to give your name and why you're calling, with no AI call needed.
- **One user, one computer.** No sign-in, roles, team seats or shared number pool. Every contact on the chosen HubSpot list is synced, whoever owns it in HubSpot.
- **Calling decides in one place.** Whether the next call is dialed is one rule, checked over every combination of setup, connection, numbers, opt-outs, audio and the call in progress.
- **Live transcript on every call.** Each call uses audio this run of the app opened. Audio left over from an earlier run is ended and reconnected, and nothing is dialed until your audio is connected, so the prospect hears you and the transcript has sound.
- **A simpler live helper.** Each tip shows the line to say and the part of your script that fits the moment.
- **Test calls stay test calls.** A test call dials only your test phone, stays a test call after you save it and pauses calling afterward instead of moving on to your lists. When your test phone is on a HubSpot contact, its calls are logged there, so you can check HubSpot with your own line.
- **The microphone never waits silently.** If macOS asks for the microphone, the Dialer says to click Allow, and after 30 seconds it pauses and says where to turn it on.
- **Blockers open their fix.** When calling is blocked, the Dialer's message has a button to the page that fixes it. Rows never set say Not set, and each page opens at its top.
- **Every screen shows the newest answer.** A slow re-read can no longer bring back an old error, check result or list state.
- **Steadier screens.** Base styles can no longer override a button, a field or a table, so every page looks the same as before, on purpose.

Updating from 1.0.1 or any 1.0.3 to 1.0.10 keeps your workspace, numbers, script and saved credentials. The team-calling and "Calls for" settings are gone; nothing else you saved changes.
Updating from 0.9.x or 0.10.x keeps your workspace and saved credentials. Updating from 1.0.0 also preserves your workspace, numbers, playbook and calling settings. macOS may ask for microphone access again.
Versions 0.2 and 0.3 start a fresh workspace and old data stays untouched in its previous folder.
Updating from 0.6.0 or 0.6.1 may require you to re-enter keys and re-import setup or login files.

# OpenDialer 1.0.10

- **Saved keys can be checked.** A key saved before this version shows Check in its Settings row, so it turns green without typing it again.
- **Follow-ups are short rows.** Each follow-up in Review is one line with Complete and Cancel; its time and reminder fold under Change.
- **The live helper knows where you are.** Tips follow the script section you are reading, and a network outage no longer uses up a list's AI budget.
- **Your last save is the save.** Outcomes, follow-ups, numbers and settings no longer stop with "changed; reload before saving".

Updating from 1.0.1 or any 1.0.3 to 1.0.9 keeps your workspace, numbers, script and saved credentials.
Updating from 0.9.x or 0.10.x keeps your workspace and saved credentials. Updating from 1.0.0 also preserves your workspace, numbers, playbook and calling settings. macOS may ask for microphone access again.
Versions 0.2 and 0.3 start a fresh workspace and old data stays untouched in its previous folder.
Updating from 0.6.0 or 0.6.1 may require you to re-enter keys and re-import setup or login files.
