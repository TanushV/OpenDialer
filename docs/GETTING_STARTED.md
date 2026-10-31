# Setup guide

[Back to README](../README.md) · [Install](INSTALL.md) · [Example script](examples/script.md) ·
[Example lead list](examples/leads.csv)

This guide takes you from a fresh install to your first call, in the order the app expects.

| Step | Page | Done when |
| --- | --- | --- |
| [1. Open a workspace](#1-open-a-workspace) | First screen | You see the Dialer. |
| [2. Connect calling and AI](#2-connect-calling-and-ai) | Settings | The Calling and AI cards say **Done**. |
| [3. Set calling hours and the do-not-call list](#3-set-calling-hours-and-the-do-not-call-list) | Settings | Both cards say **Done**. |
| [4. Add your script](#4-add-your-script) | Script | The checker says **Looks good** and you saved. |
| [5. Import leads](#5-import-leads) | Leads | The list shows **N of N ready to call**. |
| [6. Get phone numbers ready](#6-get-phone-numbers-ready) | Numbers | At least one number is in limited use. |
| [7. Make calls](#7-make-calls) | Dialer | You are on a call. |
| [8. After calls](#8-after-calls) | Review | You see your summary and what to practice. |

**Before you start, have:** a Telnyx account (or the calling setup file your admin gave you), a ChatGPT plan with
Codex signed in on this computer, an OpenRouter API key for live help, your do-not-call download for the area codes
you call, and a headset.

**Just looking?** Choose **Try a simulated workspace** on the first screen. Calls, carriers and AI are simulated,
so you can click through everything without accounts or costs.

## 1. Open a workspace

- **Working alone:** choose **Start a local workspace**. Your data and keys stay on this computer.
- **Joining a team:** choose **Connect to a server**, enter the HTTPS address from your admin and sign in. Your
  admin sets up calling and AI for everyone; skip to [step 4](#4-add-your-script) unless you are the admin.

## 2. Connect calling and AI

Open **Settings**. It is one page of cards. Each card shows **Done** or **To do**.

### Calling card

Use **one** of these:

- **You have a calling setup file** (the usual case for a local workspace): click **Import calling setup (.json)**
  and choose the file prepared for this computer. Wait for **Connected**. That is all: no tunnel, password or
  terminal command. Each computer needs its own file; do not copy one computer's workspace to another.
- **You configure Telnyx yourself:** paste the Telnyx API v2 key, Voice API connection ID, webhook public key and
  webhook address, then save. See [provider accounts](../deploy/runbooks/providers.md) for what each one is.

Keep the app open and the computer awake while calling. Callbacks cannot reach a sleeping laptop.

### AI card

1. **ChatGPT** (research and per-lead scripts): click **Use the ChatGPT login on this computer**. It appears when
   Codex is signed in here. Otherwise click **Import Codex login** and choose your `auth.json` file
   ([how to get it](#connect-chatgpt-with-a-login-file)). The card then shows your account and plan.
   Research and scripts run on GPT-6.1 Terra at maximum effort, included in your plan.
2. **Live help key (OpenRouter, Cerebras):** paste your OpenRouter key and click save. It powers the live helper
   during calls and the notes after each call.

No ChatGPT plan? Open **Use an OpenAI API key instead of ChatGPT** on the same card. That route is billed per use.

## 3. Set calling hours and the do-not-call list

### Calling hours card

1. Set **Start (hour)**, **End (hour)** and **Attempts per lead**. Hours are in each lead's own time zone; new
   workspaces start at 8:00 to 21:00.
2. Click **Confirm our business is allowed to call these leads** once.
3. Tick **Approved for live calls** and click **Save calling hours**. Until you do, nothing is dialed for real.

### Do-not-call list card

Paste the numbers from your do-not-call download, one per line, choose **National** or **State**, enter the area
codes it covers and a reference (for example your download ID), then click **Validate & publish dataset**.
Leads in area codes without a current list are not called. People who opt out during a call are blocked forever.

### More settings

Everything else has a sensible default and is folded under **More settings** at the bottom of the page:

| Section | What it is for |
| --- | --- |
| AI budget | A spending cap per lead list for paid AI (live help, call notes, or the API route). ChatGPT research costs $0 here. It never blocks calling. |
| Campaign goal and team schedule | What counts as a win (for example a meeting held) and when your team calls. |
| Do-not-call checks and consent | Extra checks before each call, such as state do-not-call lists. |
| Privacy and spending rules | Which paid services the app may spend on. |
| Callbacks | Who answers when a prospect calls back. |
| Call scoring | The rubric used to score calls. |
| Work time | How your working time is counted for statistics. |
| Billing records | Carrier and AI charges. |
| Learning experiments and trackers | Advanced testing and planning assumptions. |
| Users, backups and recovery | Team members, backups and restore. |
| Workspace and app updates | Switch workspace and install updates. |

When you think you are done, click **Check readiness** at the top of Settings. It lists anything still blocking
live calls in plain words.

## 4. Add your script

Open **Script**. Paste your script into the editor or click **Open .md file**. The right side checks it as you
type and shows how each section will be used. Fix anything it lists by line, then click **Save script**.
Start from the [example script](examples/script.md) if you are writing one from scratch.

### The format

```markdown
# Opening
- Hi {first_name}, this is {rep} with {seller}. {{ one specific, recent reason we're calling {company} }}
- Hi {first_name}, {rep} from {seller} here. {{ a short line linking their business to faster follow-up }}

# Screening
{rep} from {seller}, calling about customer follow-up.

# Gatekeeper
Who looks after customer follow-up for {company}?

# Discovery
- How do you follow up with customers after a first visit today?
- What happens when a lead comes in after hours?
- Who else weighs in on changes to that process?

# Offers
## Follow-up review
A free 20-minute review of how your team follows up.

## Pilot
{{ how a two-week pilot would fit their business }}

# Objections
## Not interested
Fair enough. Is that because it's handled, or because now isn't the time?

# Close
- Would a 15-minute call later this week be useful?

# Facts
- Setup takes under two weeks.

# Never say
- Guaranteed results
```

| Part | Meaning |
| --- | --- |
| `# Opening`, `# Screening`, `# Discovery`, `# Close` | Required sections. |
| `# Gatekeeper`, `# Offers`, `# Objections`, `# Facts`, `# Never say` | Optional sections. |
| `- bullet` | An option. The AI picks the best opening, up to three discovery questions, a lead offer and a backup, and so on, for each lead. |
| `## Heading` | One offer or one objection, with its text underneath. |
| `{{ instruction }}` | A line the AI writes for each lead from research, following your instruction. |
| `{first_name}` `{name}` `{company}` `{rep}` `{seller}` | Filled in for you. Every opening needs `{rep}` and `{seller}`; Screening needs `{seller}`. |
| `# Facts` | True things about your product the AI and live helper may say. |
| `# Never say` | Phrases the AI must never use. |

Text outside `{{ }}` is said exactly as you wrote it. The AI never invents facts, prices or promises; every line it
writes must cite something it found or a column from your lead list.

**Changing the script later:** save again. Leads already prepared are rewritten for the new version in the
background, and stay callable meanwhile.

Below the editor, **What's working** shows which openings and questions do best over time, **Call statistics**
shows your numbers, and **What you sell** is a short description of your business the live helper can use.

## 5. Import leads

Open **Leads** and scroll to **Import a list**.

1. Type a **List name** (for example "Bay Area dentists"). Names keep lists, budgets and progress apart.
2. Paste your CSV into **CSV lead data** or click **Choose CSV file**, then click **Import leads**.

### The CSV

```csv
name,phone,company,domain,state,timezone,title,notes
Jordan Lee,+14155550191,Example Coffee Roasters,example.com,CA,America/Los_Angeles,Operations Manager,Asked about weekend staffing
```

- **Required:** `name` and `phone` (with country code, like `+14155550191`).
- **Helpful:** `company`, `domain` (their website), `state` (`CA` or `California`) and `timezone`
  (like `America/Los_Angeles`). Without a time zone or state, the app estimates them from the area code.
- **Anything else** (title, notes, size, source) is given to the research agent as context.

Download the [example lead list](examples/leads.csv) to see the format.

### Research and scripts

After import, the **Research and scripts** panel at the top of Leads shows each list's progress. AI agents work in
small groups of leads: each one searches the web using the whole row, saves facts with the exact quote from the
page (the app checks the quote is really there), then writes that lead's script from yours. A lead cannot be
called until its script is done. Expect a minute or two per small group.

- **N with their own script:** ready, with lines written from research.
- **Skipped by the agent:** the agent found a reason not to personalize (for example the business has closed).
  Open **Why N leads were skipped** to read each reason. Skipped leads are called with your script as written.
- **Open any lead** with the arrow on its row to read its script, what the agent found, and the sources.
- **Agent settings** sets **Leads per agent** and **Agents at once**. The defaults suit most lists.

Contacts at the same company are never called back to back, and when one books a meeting the others are held.

## 6. Get phone numbers ready

Open **Numbers**. Each number goes through the same short path before it is used: **Purchased → Identity →
Callback tested → Limited use → Healthy**. Nothing is bought or started automatically.

**Numbers you already own on Telnyx**

1. Click **Sync existing numbers**.
2. For each number, click **Configure voice route**.
3. Call the number from another phone to prove callbacks reach you.
4. Click **Review number evidence**, then **Start or resume limited use**.

**Buying new numbers for a list**

1. Under **Plan numbers for list**, choose your list and click **Plan state coverage**. It shows how many calls the
   list needs, how many your numbers can handle, and what to add by state.
2. Click **Find numbers in [state]**, then **Find available numbers** to see exact rental prices for suggested
   local area codes, or type your own area code.
3. Under **Add numbers with approval**, set how many numbers and how much you allow, click **Save number limits**,
   then **Approve number rental** for the exact quote you want. Zero limits block all purchases.
4. Take each new number through the setup path above.

The dialer keeps each lead on the same caller number and prefers ready numbers in the lead's state.

## 7. Make calls

Open **Dialer**.

1. Open **Check audio** once to pick your headset and test it.
2. Click **Go ready**. Press **Space** (or **Next call**) to call the next lead the scheduler picks. To look first,
   click **View next lead**, read it, then **Start call**.
3. Read the lead's script. Lines the AI wrote for this lead are highlighted; other options are folded underneath.
4. The **live helper** above the script suggests what to ask next when it has something useful, and may stay quiet.
   Press **F1** or click **Private help** to ask it directly. **Mute tips** silences it.
5. If the prospect asks not to be called (or presses 9), click **Opt out** right away.
6. Click **Hang up**, then choose the outcome: **No answer**, **Voicemail**, **Gatekeeper / wrong person**,
   **Talked: not interested**, **Talked: callback requested** or **Meeting booked** (more under **More outcomes**).
   Bookings and callback times ask you to confirm.
7. Tick **Pause after call** to stop after this one, or click **Pause dialing** any time.

The scheduler only calls within calling hours, respects attempts and the do-not-call list, and decides when
another try is worthwhile. There is no automatic double dial.

## 8. After calls

Open **Review**.

- **Coaching** is what to practice and how. After each call the review agent adds one or two points from that
  call, written as what to work on and one concrete step. Add your own goals in the box below it.
- **Follow-ups** lists agreed callbacks to confirm, reschedule, snooze, assign or complete.
- **Past calls** open the summary, outcome and notes. Expand **Correct outcome** to fix a saved result.

## Updating the app

**Settings → More settings → Workspace and app updates → Check for updates**, then **Install signed update**.
Quit and reopen the app when it finishes. Your workspace is kept.

## If something is blocked

| Problem | What to do |
| --- | --- |
| **Next call** does nothing | Leads may still be researching (see Leads). Otherwise click **Check readiness** in Settings: it names the blocker, such as calling hours not approved, no do-not-call list for an area code, or no ready number. |
| A lead was skipped | Open **Why N leads were skipped** on Leads. Fix the row and import it again, or call it with your script as written. |
| ChatGPT is not connected | Settings → AI → **Use the ChatGPT login on this computer**, or import the login file. |
| A number cannot call | Finish its setup in Numbers: voice route, a real callback test, then limited use. Syncing alone is not enough. |
| Calling says it is not connected | Keep this computer awake and the app open. Resolve any unfinished calls, then click **Reconnect** in Settings → Calling. |
| A key fails after saving | Check the account's balance and settings with the [provider guide](../deploy/runbooks/providers.md). A saved key alone does not prove the service works. |
| No microphone | Allow microphone access for OpenDialer in your system settings, then pick the headset under **Check audio**. |

For backups or interrupted calls, see [recovery](../deploy/runbooks/recovery.md).

## Details

### Connect ChatGPT with a login file

Use this when Codex is not signed in on the computer running OpenDialer, or on a team server.

1. On a trusted computer with [Codex installed](https://learn.chatgpt.com/docs/auth), run:

   ```sh
   codex -c cli_auth_credentials_store='"file"' login
   ```

   Choose ChatGPT and finish signing in.
2. Find `auth.json` in the `.codex` folder in your home folder (`~/.codex/auth.json` on Mac,
   `%USERPROFILE%\.codex\auth.json` on Windows). On a Mac file picker, press **Command–Shift–G** to type the path.
3. In OpenDialer, open **Settings → AI → Import Codex login** and choose the file.

OpenDialer keeps its own copy and refreshes it while in use. A revoked or expired login needs a fresh import.
**Disconnect ChatGPT** removes OpenDialer's copy only. On a team server the file goes to that server, so only import
into a server you trust, and delete temporary copies of the file.

### Keys and privacy

- Keys are saved encrypted on the computer running your workspace, with no Keychain or password prompts.
- Calling setup and Codex login files contain credentials: keep them private.
- There is no call recording. Raw speech, quotes and helper text are deleted within five minutes; summaries keep
  only paraphrased notes.
- On the shared webhook setup, audio goes directly to Telnyx and the relay carries only call control events,
  never audio, transcripts, leads or scripts.

More: [security and data handling](policies/security.md) · [team server and direct webhooks](../deploy/runbooks/deploy.md).
