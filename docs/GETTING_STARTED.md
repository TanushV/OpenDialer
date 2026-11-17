# Setup guide

From install to your first call. Each step names the page and the button.

## 1. Install and open

Download the Mac `.dmg`, Windows `.exe`, or Linux `_amd64.deb` from the [latest release](https://github.com/TanushV/OpenDialer/releases/latest).
The Linux package targets Ubuntu 24.04 LTS on 64-bit Intel or AMD. From the download folder, run
`sudo apt install ./OpenDialer_2.1.8_amd64.deb` using the filename you downloaded, then open **OpenDialer** from the app menu.
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
6. **Test phone.** Type your own number and click **Save and check**, then **Test call**. Select **Call test phone** in
   the Dialer at any hour. When the outcome is saved, the Dialer returns to Start calling.
7. **HubSpot.** In **Settings → HubSpot Service Key**, save and check the account's Service Key if your calling
   setup did not include it. Click **Choose HubSpot owner** and select the rep by name and email on that rep's
   computer. Owner selection uses the key's `crm.objects.owners.read` scope. The owner attributes calls and new
   contacts created by that rep. Each shared profile's assigned segment still controls what it can import from
   HubSpot. Add the **RealValidito key and secret** to screen synced contacts. Treg looks up missing numbers.
8. **Test list.** Under **Test list**, select **Open test list CSV** and choose a CSV with a `phone` column and up to
   50 approved test phones. Add `name`, `company`, and `domain` for personalized scripts. Click **Save test list**.
   To prepare scripts, open **Leads → Research and scripts** and choose **Generate scripts** for **Test calls**.
   Back in Settings, select **Open test list in Dialer**, then **Run test list**. The Dialer calls each test phone
   once per run and pauses when it finishes. Begin another run from Settings. Calling hours, retry spacing and the
   app's call timer are lifted for the test list. Carrier limits and opt-outs still apply.

## 3. Numbers

Shared caller IDs appear on **Numbers** after your calling profile connects. Each rep uses the existing pool with a
limit of up to 90 outbound attempts per number per rolling 24 hours. Incoming callbacks use no outbound capacity.
Standalone workspaces can add owned numbers under **Numbers** and test their routes there.

## 4. Script

Open **Script**. Use **Open calling script (.md)** or paste your Markdown script (start from the [example](examples/script.md)).
The preview shows the sections, options and wording in source order. Click **Save script** to use the new version on
future calls. **How to write it** in the editor includes a question example.

- Use `#` for sections. Include `# Opening` and `# Close`.
- Use `##` for named alternatives, with each branch's spoken wording beneath it. Put Hire, Funding, and other
  independent branches under separate option headings.
- For one path per lead, add a `# Paths` section with entries such as `- founder: Use for founders who sell`.
  Optionally mark one path with `(default)`. Tag a named option with `[path: founder]` or share it
  across several paths with `[path: founder, sales-leader]`. Keep objections and shared options untagged.
- Opening, Gatekeeper, Discovery, Pitch, and Close also accept bullets as options. Indent a follow-up line to keep it
  with its bullet. Use one option style per section.
- Start rep directions with `>`. Put shared directions before the options, and put specific directions inside an option.
  Indent directions inside a bullet. Use `<!-- note -->` for draft comments. An inline `#` is spoken text.
- `{first_name}`, `{company}`, `{rep}` and `{seller}` fill in known fields. `{{ ... }}` tells the agent what to write for
  each lead. **Add trigger list** adds the reasons research may use for calling now.

## 5. Leads

On **Leads**, name a list and open a CSV with a phone or mobile number column. **Copy recommended CSV headers**
provides an optional template. The first-row preview shows the mapped name, number, company and phone status. Choose
the dialing column if your export includes several numbers. Use `|` between account signals and `;` between dates
or links. Research and scriptwriting can read imported columns even when they have no mapping. Reimport an older
CSV to restore columns that earlier versions omitted. **Sync a HubSpot list** is also available with a spend limit
for number checks. If HubSpot has no lists yet, make
one in HubSpot under Contacts → Lists. In **Research and scripts**, click **Generate scripts** for the list you want
personalized. Importing a list or saving the master leaves generation idle. By default, a prospect call needs a current
prepared script. To dial a list using the saved master, open **Your lists → Dialing settings**, choose **Dial only · use
master script**, and save. No lead preparation is needed for that list. If you want to call only that list, turn off
**Dialer → All active lists** and check the list by name. A shared lead still needs preparation when the selected calling
lists include a prepared-script list for that lead. The Leads table shows script and calling status together. The header
checkbox selects the whole filtered search, including leads beyond the loaded page. Uncheck individual rows to exclude them.
Open a lead to see its generated script and selected path. If the inputs change, **Last generated script · Stale**
shows the earlier version while a fresh script is needed for calls. Old saved selections can be reconstructed when
their matching master is still available.

In **Dialing settings**, a list can set its own attempts per lead. Leave both cadence fields blank to inherit the workspace
settings. Enter one wait in days between each pair of attempts, such as `2, 3` for three attempts on days 0, 2 and 5. Each
day is 24 hours. Custom waits disable the quick final retry. Call attempts are shared across memberships. When lists overlap,
the lowest attempt cap and longest applicable wait apply. Moving a lead keeps its attempt history and any wait already
scheduled. Calling windows, suppression, screening, and number capacity still apply.

**Remove list** frees its name for a fresh import and clears the preparation of leads that only belonged to that
list. Contacts with another active membership stay there. Call receipts and opt-outs remain. For an older archived
list, use **Show removed lists → Delete removed list** before reusing its name.

CSV lists can use the shared number pool with or without a HubSpot key or membership in the assigned segment.
With a saved key, the Dialer looks for the chosen CSV contact's logged outbound calls before offering a call and
applies its known history to the list's cadence. Lead details show the result of that check.
With a key saved, a connected conversation and saved outcome link the CSV lead to a matching HubSpot contact by email
or phone and log the call. A new contact is created when there is no match. OpenDialer leaves an existing contact's
owner alone and attributes the call to the owner selected on the rep's computer. If several contacts share the dialed
number, add the person's email to the CSV, reimport, and retry the write under **Settings → Needs attention**. Unanswered
calls to unlinked leads and test calls stay on the computer. If you later sync a linked prospect from a HubSpot segment,
the app keeps the same lead and call history and applies the usual checks for HubSpot imports.

## 6. Call

Assign each company to one rep. After an outbound attempt, other prospects at that company in the rep's workspace
wait until the next local calling day, even if they are on another list. OpenDialer uses the lead's confirmed time
zone or infers it from state and area code. Keep the company ID, domain or name consistent across lists.
Incoming callbacks and requested calls are handled separately.

On **Dialer**, the paused screen says how many leads can be called now, or why none can (for example, everyone is outside
calling hours). Press **Start calling** or Space.
The left Calling context panel shows the prospect's role, company description and signal when available.

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

In **Where calls end**, **Export call statistics** saves a ZIP with call outcomes, observed script events, the
structure each call used, and counts for each stage. Test calls are optional. The files contain metadata, including
section and option titles, and omit transcript text and contact details.

## When something is blocked

Read the reason and next step in the Dialer's status bar. For example, **Open Numbers** shows caller-ID availability,
and **Open Settings** takes you to screening setup. The Dialer retries while calling stays on.
