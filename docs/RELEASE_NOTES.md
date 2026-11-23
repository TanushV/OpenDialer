# OpenDialer 2.4.3

- **Voicemails need no callback number.** A message only has to name you and your business. A prospect who calls your caller ID back reaches you, or shows up as a missed callback. `{callback_number}` still works if you want to read the number out. Scripts saved before 2.4.2 can be edited again without adding it.
- **Calling hours show what is enforced.** A workspace saved with an 8:00 start now shows 9:00 to 17:00, the hours calls actually go out, and saves without an error.
- **Also includes the 2.4.2 changes.** Test calls with your script and live helper, End as voicemail with per-call voicemail advice, one script path per lead, weekday 9:00 to 17:00 calling in each prospect's time zone, HubSpot opt-outs for every rep, and transcripts that are never kept.

Updates from 1.0.x through 2.4.2 keep your workspace. Updates from 0.9.x or 0.10.x keep your workspace and saved credentials. Versions 0.2 and 0.3 start a fresh workspace and leave earlier data on disk. macOS may ask for microphone access again. Updates from 0.6.0 or 0.6.1 may require you to re-enter keys and re-import setup or login files.

# OpenDialer 2.4.2

- **Test calls work like real calls.** A test call shows your saved script, transcribes the conversation and gets live helper replies. Practice lines such as "stop calling" never add a hold, and outreach holds on the same number no longer block your test phone.
- **You decide on voicemail.** Voicemail detection is off, so the Dialer shows Connected as soon as a call is answered. Press Space for End as voicemail, then L if you left a message or N if you did not. Ending a mailbox in the first 7 seconds frees you at once while the line closes at 7 seconds, which keeps you clear of Telnyx's short-call limits.
- **Know when to leave a message.** Each call says whether to leave a voicemail and why: first and final attempts, requested callbacks and people you have spoken with. Change the rule per list in Dialing settings. Voicemail wording must name you and your business.
- **One script path per lead, shared content intact.** Each lead gets the one path that fits its role and verified signal. Untagged options beside path options, such as meeting times or shared responses, stay on every path as written, and the Script preview says what each lead keeps and picks.
- **Portable finished scripts.** A completed CSV keeps `{rep}` and `{seller}`, so each rep who imports it sees their own name and business. Optional script provenance columns travel into call statistics, and a file from a newer generator asks you to update.
- **Calling hours.** Outreach calls go out Monday to Friday, 9:00 to 17:00 in each prospect's time zone, never on US federal holidays. Requested callbacks are not limited. A number with no usable time zone is called only while every US zone is open.
- **Opt-outs shared through HubSpot.** Every rep pulls all HubSpot opt-outs, including for CSV numbers, at Start calling, after imports and every 15 minutes. Calling stops with a clear fix if they cannot sync for over 9 business days.
- **Transcripts are not kept.** Live transcription only feeds the script follower and helper during a call and is deleted when the call ends. Settings can turn live transcription and the helper off. The after-call summary is gone; the next call's brief uses the outcome and your note.
- **Callbacks show their script**, and the one-time callback check now names its number instead of "One test call left".
- **Review shows today's carrier traffic**: the share of short calls and of calls ended while ringing.

Updates from 1.0.x through 2.4.1 keep your workspace. Updates from 0.9.x or 0.10.x keep your workspace and saved credentials. Versions 0.2 and 0.3 start a fresh workspace and leave earlier data on disk. macOS may ask for microphone access again. Updates from 0.6.0 or 0.6.1 may require you to re-enter keys and re-import setup or login files.

# OpenDialer 2.4.1

- **Test calling at any hour.** Use **Test call** in the Dialer or open a saved test list from Settings. Each attempt requires a fresh action and uses the usual Telnyx and audio controls.
- **Keep test activity out of outreach.** Test calls skip HubSpot sync, lead lists, research, scripts, and outreach attempt limits. Prospect call history stays intact even when a test phone matches a prospect. Opt-outs still apply, and saved test lists carry over.
- **Also includes the 2.4.0 changes.** Import finished scripts in a lead CSV, hang up while a call is connecting, and hear a voicemail greeting before choosing when to end the call.

Updates from 1.0.x through 2.4.0 keep your workspace. Updates from 0.9.x or 0.10.x keep your workspace and saved credentials. Versions 0.2 and 0.3 start a fresh workspace and leave earlier data on disk. macOS may ask for microphone access again. Updates from 0.6.0 or 0.6.1 may require you to re-enter keys and re-import setup or login files.

# OpenDialer 2.4.0

- **Import finished scripts from CSV.** Add a `script` column with plain text or multiline Markdown. The app opens those scripts in Leads and Dialer and marks them ready immediately. Large files import into one list.
- **Place one call per action.** Start calling places one attempt. Dial now or a fresh Space press starts the next. After an outcome or a restart, the Dialer waits for you.
- **Hear the voicemail greeting.** The Dialer hands ringback over to call audio. When Telnyx detects voicemail, the call stays open through the greeting. Press Space or select End as voicemail when ready.
- **Hang up while a call is connecting.** Hang up can cancel an attempt before it reaches the carrier and can end the local audio leg while the carrier is still responding.

Updates from 1.0.x through 2.3.0 keep your workspace. Updates from 0.9.x or 0.10.x keep your workspace and saved credentials. Versions 0.2 and 0.3 start a fresh workspace and leave earlier data on disk. macOS may ask for microphone access again. Updates from 0.6.0 or 0.6.1 may require you to re-enter keys and re-import setup or login files.

# OpenDialer 2.3.0

- **Hear outbound calls ring.** The Dialer plays a local ringback tone until the call answers or stops. It shows the answer while Telnyx identifies a person, voicemail, or call screening.
- **Hang up while dialing.** Click Hang up or press Esc before or after answer. A late answer during Skip stays available for the rep to classify.
- **Pause script generation by list.** Resume keeps finished scripts for the current master. If the master changes during generation, old results cannot publish and resumed work uses the new master.
- **Read RealValidito DNC credits correctly.** Settings displays the balance and gives specific reasons when the provider rejects the credentials or IP address.
- **Import an existing ChatGPT session.** OpenDialer verifies the imported login without forcing a token refresh and keeps the prior login if verification fails.

Updates from 1.0.x through 2.2.4 keep your workspace. Updates from 0.9.x or 0.10.x keep your workspace and saved credentials. Versions 0.2 and 0.3 start a fresh workspace and leave earlier data on disk. macOS may ask for microphone access again. Updates from 0.6.0 or 0.6.1 may require you to re-enter keys and re-import setup or login files.

# OpenDialer 2.2.4

- **Import CSVs with more header formats.** Use common name, phone, company, and location headers. Preview the dialing column, copy recommended headers, and see why individual rows were skipped. Research and scriptwriting can read custom columns.
- **Remove and re-add a list.** Removing a list frees its name. Reimporting starts fresh while saved calls, opt-outs, and other list memberships remain available.
- **Check HubSpot call history before dialing CSV leads.** Logged outbound calls count toward the list's cadence when a HubSpot key is configured.
- **Generate one researched script path.** Define paths in Markdown and keep shared lines and objections in their original order. Leads shows the generated script and its last saved version when stale.
- **See the prospect brief in Dialer.** The left panel shows the prospect's role, company description, and signal when available.
- **Control list selection and cadence.** Select all matching leads across pages, choose prepared or dial-only scripts per list, and set attempt caps and waits. Calls to others at the same company wait until the next local calling day.

Updates from 1.0.x through 2.2.3 keep your workspace. Updates from 0.9.x or 0.10.x keep your workspace and saved credentials. Versions 0.2 and 0.3 start a fresh workspace and leave earlier data on disk. macOS may ask for microphone access again. Updates from 0.6.0 or 0.6.1 may require you to re-enter keys and re-import setup or login files.

# OpenDialer 2.2.3

- **Filter and select all leads.** The list picker loads independently of callability counts. Select all includes every lead matching the current list and search, across all pages.
- **Choose how each list dials.** The list's Dialing settings can use the master script directly or require a prepared script. Set a per-lead attempt cap and the wait between each attempt. A lead in several active lists keeps one attempt history.
- **See when scripts are ready.** Leads shows script preparation status and refreshes an open lead when its script finishes. Lists that require preparation call only leads with a current script.
- **Review carrier events with context.** Startup archives confirmed stale rep-audio events. Remaining recovery entries show the event type and receipt time.

Updates from 1.0.x through 2.2.2 keep your workspace. Updates from 0.9.x or 0.10.x keep your workspace and saved credentials. Versions 0.2 and 0.3 start a fresh workspace and leave earlier data on disk. macOS may ask for microphone access again. Updates from 0.6.0 or 0.6.1 may require you to re-enter keys and re-import setup or login files.

# OpenDialer 2.2.2

- **Get a reply sooner.** The live helper listens for a shorter pause when the prospect's thought sounds complete. A quick suggestion appears while the model works, and the model can improve it when ready.
- **Get help with whatever comes up.** The helper reads the script in the order you wrote it and suggests what to say. It can use a line, adapt it, do estimate math from the conversation, or answer a different question.
- **Keep questions easy to read.** The Script preview and live helper preserve line breaks in question branches. Indented directions stay with their question. The editor includes a Markdown example.
- **Use the whole CSV for research.** OpenDialer recognizes common export headers. The research agent reads all columns in each CSV row, including fields outside the column map.
- **Call CSV lists with a shared profile.** Generate scripts and dial the imported list directly. Connect a HubSpot owner in Settings to attribute calls to the rep. After a connected CSV call, OpenDialer links or creates the contact and logs the outcome in HubSpot when configured.

Updates from 1.0.x through 2.2.1 keep your workspace. Updates from 0.9.x or 0.10.x keep your workspace and saved credentials. Versions 0.2 and 0.3 start a fresh workspace and leave earlier data on disk. macOS may ask for microphone access again. Updates from 0.6.0 or 0.6.1 may require you to re-enter keys and re-import setup or login files.

# OpenDialer 2.2.1

- **Get help off script.** The live helper uses or adapts a relevant script line or suggests what to say next. Estimates use the inputs from the conversation. Use **Show in script** to open the selected line in place.
- **Get the tip sooner.** The helper starts after the prospect pauses with one short request. If the model takes too long or returns unusable text, a local reply appears without waiting or retrying.
- **See the current lead count.** The Leads badge counts leads in active lists. A lead in multiple active lists counts once, and removed leads stay in history.

Updates from 1.0.x through 2.2.0 keep your workspace. Updates from 0.9.x or 0.10.x keep your workspace and saved credentials. Versions 0.2 and 0.3 start a fresh workspace and leave earlier data on disk. macOS may ask for microphone access again. Updates from 0.6.0 or 0.6.1 may require you to re-enter keys and re-import setup or login files.

# OpenDialer 2.2.0

- **See why a call didn't start.** The Dialer keeps a visible warning with the exact reason and a link to the relevant settings when one exists. Carrier refusals show the Telnyx error code when available.
- **Know whether it's safe to retry.** A refused dial closes without asking for an outcome. If the dial response is lost, OpenDialer keeps the original attempt and checks for its result without redialing.
- **See when a test didn't connect.** A busy destination, no answer, or a carrier block stays visible. The Settings button is labeled **Prepare test call**. Use **Test call** in the Dialer to place it directly.

Updates from 1.0.x through 2.1.12 keep your workspace. Updates from 0.9.x or 0.10.x keep your workspace and saved credentials. Versions 0.2 and 0.3 start a fresh workspace and leave earlier data on disk. macOS may ask for microphone access again. Updates from 0.6.0 or 0.6.1 may require you to re-enter keys and re-import setup or login files.

# OpenDialer 2.1.12

- **Start a fresh test every time.** An earlier test result no longer pauses a new call while the Dialer refreshes.
- **Check audio on a single test call.** The line connects on answer without automatic voicemail detection. Test lists continue to use voicemail detection for rehearsing the normal calling flow.
- **Keep test calls out of HubSpot.** Test calls, outcomes and summaries stay on this desktop, including when the saved test number matches a HubSpot contact.

Updates from 1.0.x through 2.1.11 keep your workspace. Updates from 0.9.x or 0.10.x keep your workspace and saved credentials. Versions 0.2 and 0.3 start a fresh workspace and leave earlier data on disk. macOS may ask for microphone access again. Updates from 0.6.0 or 0.6.1 may require you to re-enter keys and re-import setup or login files.

# OpenDialer 2.1.11

- **Start a test call directly in the Dialer.** Test call dials the saved test phone once, even outside prospect calling hours. OpenDialer pauses after the test. If the saved phone needs attention, the button opens Settings so you can review it or save another test phone.

Updates from 1.0.x through 2.1.10 keep your workspace. Updates from 0.9.x or 0.10.x keep your workspace and saved credentials. Versions 0.2 and 0.3 start a fresh workspace and leave earlier data on disk. macOS may ask for microphone access again. Updates from 0.6.0 or 0.6.1 may require you to re-enter keys and re-import setup or login files.

# OpenDialer 2.1.10

- **Review a test phone hold in Settings.** The Test phone row shows why the number is blocked. If you confirm there was no request to stop calls, release the temporary hold and record your reason there. Confirmed opt-outs stay in effect.

Updates from 1.0.x through 2.1.9 keep your workspace. Updates from 0.9.x or 0.10.x keep your workspace and saved credentials. Versions 0.2 and 0.3 start a fresh workspace and leave earlier data on disk. macOS may ask for microphone access again. Updates from 0.6.0 or 0.6.1 may require you to re-enter keys and re-import setup or login files.

# OpenDialer 2.1.9

- **Repeat a test call after practicing an outcome.** Saving Wrong number or Wrong person on a standalone test contact keeps the outcome in call history. Settings explains any existing block on the saved phone before opening the Dialer.
- **End a test cleanly.** Ending a single test releases its unused reservation and pauses calling. A test list reports blocked phones after it has called the eligible ones.
- **Get live suggestions sooner.** The helper checks the prospect’s audio for a pause while transcription runs. It can show the chosen script line while the call is still active, even when the rep starts speaking.

Updates from 1.0.x through 2.1.8 keep your workspace. Updates from 0.9.x or 0.10.x keep your workspace and saved credentials. Versions 0.2 and 0.3 start a fresh workspace and leave earlier data on disk. macOS may ask for microphone access again. Updates from 0.6.0 or 0.6.1 may require you to re-enter keys and re-import setup or login files.

# OpenDialer 2.1.8

- **Keep the Markdown script in order.** The preview and Dialer preserve your headings and option order, including custom sections.
- **Follow the rep's speech.** The Dialer highlights the last confidently matched line and scrolls inside the script panel, including after a manual scroll.
- **Export call statistics.** Review downloads the call outcomes, observed talk paths, script structures and stage statistics for the scripts actually used.

Updating from 1.0.x through 2.1.7 keeps your workspace. Updating from 0.9.x or 0.10.x keeps your workspace and saved credentials. Versions 0.2 and 0.3 start a fresh workspace and old data stays untouched. macOS may ask for microphone access again. Updating from 0.6.0 or 0.6.1 may require you to re-enter keys and re-import setup or login files.

# OpenDialer 2.1.7

- **See why calling is waiting.** When several things block the queue, the status shows the reason for its fix button first, followed by other reasons such as calling hours or retry spacing.
- **Run a multi-number test list.** Save a CSV in Settings, optionally generate personalized scripts for it in Leads, then run it in the Dialer at any hour.

Updating from 1.0.x through 2.1.6 keeps your workspace. Updating from 0.9.x or 0.10.x keeps your workspace and saved credentials. Versions 0.2 and 0.3 start a fresh workspace and old data stays untouched. macOS may ask for microphone access again. Updating from 0.6.0 or 0.6.1 may require you to re-enter keys and re-import setup or login files.

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
