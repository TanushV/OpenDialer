# OpenDialer 2.0.0 handoff: open issues and everything left to test

Written November 11, 2026, for whoever finishes 2.0.0: the owner, or an agent with no memory of the work so far. It
stands on its own. The plan of record is still [REBUILD_PLAN.md](REBUILD_PLAN.md); read its Status section first. This
file is the punch list between today and a published 2.0.0.

**Short version.** The 2.0.0 code is built and CI is green. A draft release exists and is installed on the owner's Mac.
It is not ready to publish. One defect blocks it (callbacks stop ringing when the window is off-screen), most live
checks have never run on a real call, and four planned checks cannot run as written. Nothing in section 3 is fixed
yet. The fixes wait for the owner's go-ahead.

Sections: 1 state, 2 rules, 3 issues, 4 fix order, 5 tests, 6 owner decisions, 7 commands, 8 receipts.

## 1. Where things stand

| Item | State on November 11 |
|---|---|
| Repository | `~/Documents/OpenDialer`, GitHub `TanushV/OpenDialer`, branch `main` only |
| Code on GitHub | `6b437f3`. CI green (run 42805729385) |
| Local only, not pushed | `673f36f` (evidence) and the commit that adds this file. Docs only. A push needs `scripts/check.py full` |
| Draft release | `v2.0.0`, still a draft, built from `6b437f3` (release run 42807304202). This is "candidate 4" |
| Draft assets | `OpenDialer.app.tar.gz` (+ `.sig`), `OpenDialer_2.0.0_aarch64.dmg`, `OpenDialer_2.0.0_x64-setup.exe` (+ `.sig`), `OpenDialer_2.0.0_x64_en-US.msi` (+ `.sig`), `latest.json`, `SHA256SUMS.txt` |
| Public latest | 1.0.10. The updater offers nobody 2.0.0 until the draft is published |
| Installed on the owner's Mac | 2.0.0 candidate 4, installed November 10 at 03:36 Pacific over candidate 3. Workspace data intact |
| Workspace store | `~/Library/Application Support/com.opendialer.desktop/local-3/opendialer.db`. Read it only through a copy (section 7) |
| App log | `~/Library/Application Support/com.opendialer.desktop/local-3/logs/opendialer.log` |
| Phone numbers | Six, all `limited_real_use`, `active`, route confirmed (endings 854, 693, 823, 794, 679, 158) |
| Calling state | Paused. No test session open. One suppression in the store, none on the test line |
| Test line | 203-821-1101, the owner's phone. Allowlisted in code (`TEST_LINES`, `services/backend/app/platform/config.py:75`) |
| HubSpot | Portal 51457381, 4,759 contacts. Test contact "OpenDialer Test", id 261425233976, phone +12038211101. Segment "testing" (id 14, 50 contacts, none with a number) |

Line numbers in this file are as of `6b437f3`.

### What has already passed live

| Check | Build | What it proves, and what it does not |
|---|---|---|
| L1 owner answers, rep hangs up | candidate 2 | Call control and the screen. Not voice: the rep's audio was a dead leg then (fixed in candidate 3) |
| L2 owner answers, owner hangs up | candidate 2 | "They hung up · 0:25", no false block. Same voice caveat |
| Reopen with a call owed an outcome | candidate 3 | Holds across an app replacement. Not yet across a force quit mid-call (L10) |
| L14 HubSpot sync | candidate 3, scratch copy | Segment read and sync work (50 added, second sync 0/0/0). Not yet run in the installed workspace |
| R11.2 number stuck as "purchased" | candidate 4 | Number ending 158 finished setup by itself on start |
| HubSpot write path | November 9, direct | Call record and `phone_status` write-back reached the portal. The list read was patched to the one test contact |

Receipts: [2026-11-10-live-calls.md](evidence/rebuild/2026-11-10-live-calls.md) and
[2026-11-09-hubspot-treg-live.md](evidence/rebuild/2026-11-09-hubspot-treg-live.md).

## 2. Rules for whoever continues

These come from `AGENTS.md`, the plan and the owner's standing instructions. They are not optional.

- One worker in one thread. No subagents, no parallel sessions, no worktrees.
- Commit to `main`, no feature branches. Fast check while iterating, full check before every push, never push a
  failing full check, no `--no-verify`. Start nothing new while CI on `main` is red.
- Acceptance tests before code. A regression test before a defect fix. Every blocker in `calling/blockers.py` needs its
  exit in `EXITS` and a test in `tests/acceptance/test_blocker_exits.py`.
- Never commit credentials, app databases, recordings, build products or test caches. No AI attribution in commits,
  PRs or releases.
- Live test calls go only to 203-821-1101, and only when the owner says go. No test dials a prospect.
- Read the installed workspace only through a scratch copy. Preserve installed data.
- Do not touch Keychain or ask for an OS password.
- Paid runs (number searches, prospect research) each need their own yes and a limit from the owner.
- A simulated test is evidence about software only. Carrier behavior, audio hardware and HubSpot need their own live
  evidence. A missing key is never a pass.
- A request to diagnose or report is read-only. Code changes wait for an explicit go-ahead.
- Installing a build on the owner's Mac needs the owner's request. Candidates 1 to 4 were installed under the
  "finish to 2.0" request.

## 3. Issues

### Blocking

**I1. Callbacks do not ring when the OpenDialer window is off-screen.**

- Symptom: the app tells the backend "I can take calls" every 30 seconds. That heartbeat stops when the window is not
  on the visible desktop. After 90 seconds without it the backend stops ringing the app, so a callback gets the
  greeting and a missed-call task. The owner reported this as "the callback doesn't work".
- Evidence: on November 10 the heartbeat ran from 15:01 to 16:15:46 Pacific, then stopped
  (`reachable.at = 2026-11-11T00:15:46Z`). It was still that value at 09:26 UTC on November 11, nine hours later, with
  the app open and the backend alive (about 178 relay polls an hour). The display stayed on until 21:06, so sleep or
  lock did not cause the stop. The window was on another desktop (`is_off_space: true`).
- Cause: likely, not proven. The heartbeat is a page timer (`setInterval`, `apps/desktop/src/calling/useCallLoop.ts`
  lines 320 to 358). WebKit throttles or suspends timers in a window that is not visible, and macOS App Nap does the
  same to the process. The window sets no `backgroundThrottling` (`apps/desktop/src-tauri/tauri.conf.json` lines 13 to
  22), and the shell's keep-awake is `caffeinate` only (`apps/desktop/src-tauri/src/lib.rs`).
- Prove it first: put the window on the visible desktop and read `reachable.at` (section 7). It should refresh within
  30 seconds. Move the window to another desktop for 10 minutes and read again. It should go stale.
- Proposed fix: set `"backgroundThrottling": "disabled"` on the `main` window (Tauri 2, macOS 14 and later); hold an
  App Nap guard in the shell while the app is open; fix I2 in the same change. If timers still stall, move the
  heartbeat out of the page, or have the shell bring the window forward when a callback arrives.
- Open question the fix does not answer: a hidden page must also receive the call invite and play the ring. Only the
  live check B3 in section 5.3 proves that.
- Backend side: `can_ring` and `REACHABLE_SECONDS = 90` in `services/backend/app/telephony/callbacks.py` (lines 149 to
  170), route `/v1/rep/reachable` in `services/backend/app/main.py` line 546. These behave as designed.
- Tests first: a frontend test for the tick (posts while a connect is pending, never overlaps), a test that the
  window config disables throttling, then the live check. `test_callbacks_in_app.py` already covers the 90 seconds.

**I2. The heartbeat tick can overlap itself and reports late.** Same code as I1. `setInterval` fires every 30 seconds
with no in-flight guard, while one tick can wait up to 45 seconds inside `connect` (30 seconds for the microphone, 15
for registration). The "reachable" post sits behind that wait. Fix: one tick at a time, and post the current state
before trying to connect, then again if it changed.

**I3. Five things have never been proven on a real call.** The code and simulated tests exist. No live call has shown:

1. A live transcript in the Dialer since the dead-audio-leg fix (candidate 3).
2. Voice both ways: the owner hears the rep, and the rep hears the owner.
3. The live helper: one line to say plus the script part that fits.
4. The call-screener tip (R11.8).
5. A callback that rings in the app, is answered, and has audio both ways (R11.3).

These are tests A1 to A4 and B1 in section 5. Any failure is a new defect and means another candidate.

### Checks that cannot run as written

**I4. L7, L16 and L17 would block the test line for good.** They save an opt-out or a wrong-party denial. Both are
permanent by design: `release_hold` refuses them (`services/backend/app/compliance/suppressions.py` lines 101 to 117),
and the policy applies them before the test-line exemption (`services/backend/app/compliance/policy.py` lines 85 to
87). Run them against 203-821-1101 in the installed workspace and no further test call can be placed there. Options
are in section 6, decision 3.

**I5. L3, L4, L5 and L12 cannot pass as the plan words them.** Their pass lines say "next call starts", and L12 says
"20 calls in a row". A test call is one call: after it is saved or skipped the session ends and the Dialer pauses with
"Test call saved. Calling is paused; Start calling uses your lead lists." (`services/backend/app/main.py` lines 672 to
680, `useCallLoop.ts` lines 716 to 726). That rule was added on November 10 so a test never rolls into real leads.
So on the test line only the first half of each check can be seen. The automatic advance is covered by the browser
suite (`apps/desktop/tests/simple-calling.spec.ts`) and the soak, not by a carrier call. L12 becomes 20 single test
calls. The owner should accept that wording (section 6, decision 4).

**I6. The installed test lead is past its attempt limit, so every test call on it is a "final attempt".** The lead
"Owner delivery test" has an attempt count of 6 against an allowance of 4. The last three test calls were attempts 4,
5 and 6. `final_attempt` is `ordinal >= allowance` (`services/backend/app/calling/events.py` line 59), and the count
is kept per lead (`services/backend/app/calling/dial.py` lines 247 to 251). Effect: a voicemail on this lead holds the
line open (L6) and is never skipped (L5). So L6 can be tested on this lead today, and L5 needs a lead with fewer than
three attempts. This is read from code and the store, not yet seen on a call.

**I7. The installed workspace has no HubSpot lead for the test phone.** L15 to L19 need one. The L14 sync ran on a
scratch copy. The test contact is also not in segment "testing": all 50 of its contacts came back without a number,
and the test contact is the only contact in the portal with one (inferred, not looked up). The saved key cannot edit
segments, so the owner has to add the contact to a segment in HubSpot.

### Small fixes for the same build

**I8. Settings leftovers from the archived trainer.** In `apps/desktop/src/components/OptimizationPanel.tsx`: the
button says "Save number limits" but the toast says "Planning assumptions saved" (line 200); the comment on line 52
still describes StatML's card; `ADVANCED_KEYS` (lines 60 to 71) still carries six keys the panel no longer shows
(`exploration`, `horizon_hours`, `rep_minutes`, `handling_seconds`, `training_min_rows`, `auto_train`). Check the
backend settings model before deleting the keys.

**I9. Stale documents.** `docs/HANDOFF.md` describes 1.0.1 and the trainer; it now points here. `ARCHITECTURE.md`,
`VALIDATION.md`, `REQUIREMENTS.md` and `HANDOFF_AST.md` still mention StatML or the trainer and need a pass before
publishing. `RELEASE_NOTES.md` already has the 2.0.0 notes; add the known limits below to them.

**I10. The Script page still shows the script proposals panel.** `ScriptMemory` renders at
`apps/desktop/src/components/Operations.tsx` line 1172, over about 2,850 lines of experiment code in
`services/backend/app/optimization/` (`script_lab.py`, `experiments.py`, `learning.py`, `workflows.py`). It has never
been tested live, and the owner asked for a simple helper. Hide it or keep it: section 6, decision 5.

### Data to tidy (no code)

- **I11.** Test call 3 (November 10) is saved as Voicemail, but no voicemail was left. A phone screener most likely
  answered. It is a test record and reaches neither learning nor HubSpot. Leave it, or correct it in Review.
- **I12.** The Greg Sarantopoulos call of November 6 is saved as `no_answer` after 23 connected seconds. The owner
  needs to say what the right outcome is.
- **I13.** 8 of the 13 open "Incoming callback" tasks came from the owner's own test line. They can be closed on the
  owner's OK.

### Known limits to accept for 2.0, and to state in the release notes

| # | Limit |
|---|---|
| K1 | Callbacks are audio only. No transcript and no helper on an inbound call |
| K2 | A hidden window gets the ring sound and a Dock bounce. No macOS notification banner |
| K3 | A locked or sleeping Mac cannot ring. The caller gets the greeting and the rep gets a missed-call task |
| K4 | macOS asks for the microphone again after each new build (ad-hoc signature). Someone has to click Allow |
| K5 | Forwarding a callback to a phone is deferred (D30) |
| K6 | No voicemail drop. The rep speaks into the voicemail; auto-skip stays (D32) |
| K7 | The backend log holds 18 errors in total. The recent ones are realtime socket drops (code 1006) and "Relay exchange failed: reconnecting". Both recover by themselves. Watch the count during L12 |
| K8 | The do-not-call check has no key and has never run live |
| K9 | Windows installers are built by CI but have never run on a PC and are unsigned |
| K10 | Size caps are warnings, not blocks (D28) |

### Housekeeping

- Two docs-only commits are local and unpushed (section 1).
- The draft is built from `6b437f3`. Any code fix means a new draft (candidate 5) and a new install.

## 4. Proposed order of work

Needs the owner's go-ahead. None of it has started.

1. Prove I1's cause (two store reads, section 7). Write the failing tests for I1 and I2.
2. Fix I1 and I2. Fix I8. Hide the proposals panel if the owner says so (I10).
3. Refresh the stale documents (I9). Update the plan's Status.
4. `uv run python scripts/check.py full`, push, wait for green CI.
5. Rebuild the draft (candidate 5), install it on the owner's request.
6. Live sessions A to D with the owner (section 5). Record receipts.
7. Fix what the sessions find. Each code fix repeats steps 4 to 6 for the checks it touches.
8. Release checks (section 5.8), publish, final report.

## 5. Everything to test

### 5.0 Before any live session

- The owner is at the Mac. The Mac is unlocked. OpenDialer is on the visible desktop.
- A new build asks for the microphone. The owner clicks Allow.
- The owner says go. Calls go to 203-821-1101 only.
- Confirm nothing is live: calling is paused and the newest call has an outcome (section 7).
- A test call starts in Settings: Test phone row, **Test call**. Then Dialer, **Start calling**.
- **A test session is one call.** After its outcome is saved the session is over. Pressing Start calling again
  without a new Test call uses the real lead lists. Always go back to Settings for the next test call.
- Calling hours and attempt limits do not apply to the test line. An opt-out or a wrong-party denial does (I4).
- Dialer keys: Space go now or skip, Esc hang up, 1 Meeting booked, 2 Callback requested, 3 Not interested,
  4 Gatekeeper, 5 Wrong number, 6 Voicemail, P pause, S stay, Enter confirm a suggested outcome.
- An agent can drive the installed app with the computer-use `app_*` tools on `com.opendialer.desktop`. A new session
  must ask for access. When the window is on another desktop, clicks fail and keys still work.
- Each call costs about 1 cent.

### 5.1 Automated checks

Green on `6b437f3`. Run again after any change.

| Command | Covers |
|---|---|
| `uv run python scripts/check.py` | Fast tier: lint, rules, types, format, frontend unit tests, edge, backend fast tests. Runs on every commit |
| `uv run python scripts/check.py full` | Adds the Rust shell and the browser suite. Runs on every push |
| `uv run python scripts/check.py package` | Freezes the sidecars and smoke-starts them |

Tests that back the features under live test:

| Area | Tests |
|---|---|
| Callbacks in the app | `services/backend/tests/acceptance/test_callbacks_in_app.py`, `apps/desktop/tests/callback-routing.spec.ts`, `apps/desktop/tests/ringer.test.ts` |
| Blockers and their exits | `services/backend/tests/acceptance/test_blocker_exits.py` |
| Calling, voicemail skip, test session | `services/backend/tests/acceptance/test_feature_calling.py`, `apps/desktop/tests/simple-calling.spec.ts`, `apps/desktop/tests/screen-model.test.ts` |
| Audio and the call client | `apps/desktop/tests/microphone.test.ts`, `apps/desktop/tests/telephony.test.ts` |
| Live helper and screener tip | `services/backend/tests/acceptance/test_feature_live_helper.py` |
| HubSpot | `services/backend/tests/acceptance/test_crm_flow.py` |
| Review, stage drop-off | `apps/desktop/tests/drop-off.spec.ts` |
| Layout | `apps/desktop/tests/layout-audit.spec.ts` |

### 5.2 Session A: outbound calls on the test line

The agent or the owner drives the app. The owner holds the phone. About 30 to 45 minutes.

Order matters because of I6 and I7: run A7 (L6) on the current test lead first. Run A6 (L5) only after the HubSpot
test contact is synced as a fresh lead (session D setup), which the test session then prefers.

| # | Check | Steps | Pass when |
|---|---|---|---|
| A1 | Live transcript | Test call. Owner answers. Both sides talk for 30 seconds | Lines from both sides appear in the Dialer within a few seconds, while the call is live. No "Live transcription stopped for this call" |
| A2 | Voice both ways | Same call. Owner speaks into the Mac and listens on the phone, then the reverse | The phone plays what the Mac's microphone hears, and the Mac plays what the phone says. No echo loop, no one-way audio |
| A3 | Live helper | A script is saved. Owner, as the prospect, raises an objection ("we already have a vendor") and later asks about price | One tip at a time: a line to say and the numbered script part that fits. It changes as the talk moves. Nothing else on screen competes with it |
| A4 | Screener tip | Owner answers and says "Please record your name and reason for calling" | The tip reads exactly "Call screener: say your name and why you're calling, in one sentence." It appears at once. The call is not skipped as voicemail |
| A5 | L1 and L2 again, now with voice | One call the rep ends (Esc), one the owner ends | L1: "You ended the call", outcome keys work. L2: "They hung up" within 2 seconds, no false flag, no block |
| A6 | L5 voicemail skip | Needs a test lead with fewer than 3 attempts (I6). Phone screening and Live Voicemail off, so the carrier voicemail answers. Owner does not answer | The app hangs up by itself at detection and not before 7 seconds after the answer. Voicemail is saved. Then the test-call notice (not a next call, I5) |
| A7 | L6 final attempt reaches voicemail | Current test lead (its next call is attempt 7 of 4). Owner does not answer | The line stays open through the greeting. The rep speaks and hangs up with Esc. The voicemail on the phone holds the rep's words |
| A8 | Stay key | On a non-final attempt, press S while it rings or as the greeting starts | The skip is withdrawn and the line stays open |
| A9 | L3 owner ignores the call | Owner lets it ring. The app rings for 40 seconds (`outbound_ring_seconds`). Most voicemail answers sooner, which turns this into A6 or A7. For a true no-answer the phone must ring past 40 seconds | No answer is saved without a click. Then the test-call notice (I5). Record which case happened |
| A10 | L4 owner declines | Owner taps Decline. Many carriers send a declined call to voicemail | Busy or no answer saved, or voicemail handling as in A6 or A7. The saved outcome matches what the carrier reported |
| A11 | L7 opt-out during a call | **Not on 203-821-1101 (I4).** Use a second phone the owner holds, typed into Test phone. Press Opt out during the call | The call ends. The number is suppressed. A new test call to it is refused |
| A12 | L12 twenty calls | 20 rounds of Test call, Start calling, a 10 second answered call, save. Alternate who hangs up | No "Go ready", no stuck state, audio both ways on calls 1, 10 and 20, a transcript on each, the Mac stays awake, no new kind of error in the log |
| A13 | R5.13 | The owner drives a few of the calls above without help | The owner reports anything confusing. Each report is fixed or written down |

Also record, as the plan asks: whether the hourly carrier cap applied, and whether the automated notice played.

### 5.3 Session B: callbacks (L8)

Only worth running after I1 is fixed. The owner calls back any of the six numbers from 203-821-1101. About 20 minutes.
Before each case read `reachable.at` (section 7): it must be under 90 seconds old, except in B6 and B7.

| # | Case | Steps | Pass when |
|---|---|---|---|
| B1 | App open on the Dialer, visible, paused | Owner calls | The ring panel shows the caller with Answer and Decline. The ring tone plays. Answer gives audio both ways. Either side can hang up |
| B2 | App open on another page | Repeat on Leads, Review and Settings | The same panel appears on every page |
| B3 | Window hidden or on another desktop | Hide it for at least 10 minutes first, then call | It still rings: tone and Dock bounce. Bringing the window forward shows the panel, and Answer works. This is the proof for I1 |
| B4 | During a live test call | Owner calls from a second phone while a test call is up | The live call is not disturbed. The caller hears the greeting. A missed-call task appears in Review, Follow-ups |
| B5 | Not answered | Let it ring 20 seconds once. Press Decline once | The greeting plays to the caller. One "Incoming callback" task per call. The ring stops |
| B6 | App closed | Quit the app, then call | Greeting. The task is there after reopening |
| B7 | Mac locked or asleep | Lock the Mac, then call | Same as B6. This is known limit K3, checked so the limit is stated truthfully |
| B8 | A callback during auto-dial | With calling started, call in during the countdown | Outbound dialing waits while the callback rings or is live |

### 5.4 Session C: failures

Each one happens during a live, answered test call. About 20 minutes.

| # | Check | Steps | Pass when |
|---|---|---|---|
| C1 | L9 Wi-Fi off mid-call | Turn the Mac's Wi-Fi off, wait 30 seconds, turn it on | The owner's phone call ends. The app says what happened. It recovers when Wi-Fi returns. The call is owed an outcome and is not dialed again |
| C2 | L10 force quit mid-call | Force quit OpenDialer (Cmd+Option+Esc), reopen | The owner's phone call ends. The reopened Dialer shows the call owed an outcome. No second dial |
| C3 | L11 app in the background when audio connects | Press Start calling, then switch to another app at once | Audio connects, or the blocker says what to do. Tied to I1: a throttled page may fail here too |
| C4 | L13 backend killed mid-call | `pkill -f "opendialer-backend serve"` | The backend restarts. The call can be ended. The screen stays usable |

### 5.5 Session D: HubSpot

Setup, by the owner in HubSpot: put the contact "OpenDialer Test" in a segment. A new segment with only that contact
is cleanest. In the app, choose that segment under HubSpot lists and Sync. Then check the new lead's attempt count is 0
(section 7). If the sync merged it into the old test lead the count is still 6, and A6 needs a second phone instead.

Order: D1, D2, then A6 from session A, D3, D4, and D5 and D6 last.

| # | Check | Steps | Pass when |
|---|---|---|---|
| D1 | L14 in the installed workspace | Sync, then Sync again | The lead appears with the right name, number and mapped signal columns. The second sync changes nothing |
| D2 | L15 call record | Test call (the session prefers the HubSpot lead, R11.7). Save an outcome | Within a minute the contact's timeline in HubSpot shows a call with the right outcome, duration, direction and summary, and no transcript |
| D3 | L19 retry writes once | Turn Wi-Fi off right after saving an outcome, back on after 30 seconds | Exactly one call record for that call |
| D4 | L18 contacted elsewhere | Log a call by hand on the test contact in HubSpot, then Sync | `notes_last_contacted` moves and the app skips the contact. If it does not, the guard is dropped (the plan's rule). Run this after D2 and D3, since it takes the lead out of the queue |
| D5 | L16 wrong party | **Blocks the phone it runs on (I4).** Save Wrong number | HubSpot shows Wrong number and `phone_status`. The number is not redialed. The next sync does not restore it |
| D6 | L17 opt-out both ways | **Blocks the phone it runs on (I4).** Opt out in HubSpot and Sync. Opt out in the app and Sync | Both suppress. Neither sync clears it. HubSpot shows the app's opt-out |
| D7 | First paid number search (R9B.20) | Needs the owner's yes and a limit. Set "Spend limit for number checks (USD)" on a list of `a_plus` contacts (240 in the portal, about $12) | Numbers land on leads. Spend stays under the limit. Cost, coverage and misses are recorded. Then 20 real contacts, as the plan says |
| D8 | Do-not-call check (D23) | Needs the RealValidito key and secret saved in Settings | CRM leads are checked. A listed number is held and says why. Never claim this from a missing key |

Cleanup when session D is done: delete HubSpot contact 261425233976 and the test segment, and remove the test list in
the app. The owner said test data in HubSpot is fine as long as it is deleted afterward.

### 5.6 Windows (L25 to L27)

Blocked. It needs a Windows PC and a signing route (R9D.4), and neither exists. The installers are built by CI and
have never run. L21 to L24 were withdrawn with multi-user (D27); L26 and L27 shrink to one seat: install, one call to
203-821-1101, update, roll back, data kept. Recommended: ship 2.0.0 for Mac and say so (section 6, decision 2).

### 5.7 Other open plan items

| Item | What it is | Waits on |
|---|---|---|
| R9.14 | Re-prepare 20 real leads on each route; the owner judges quality; measure cost, coverage and drop rate | Owner's time and a spend OK |
| R9.15 | Re-prepare the active leads | Owner's OK after R9.14 |
| R9.19 | T1: Telnyx Caller Identity lookup on the owner's line, $0.044 | Owner's OK to use the saved Telnyx key |
| R9B.20 | Waterfall and do-not-call on the HubSpot test list, then 20 real contacts | D7 and D8 |
| R9E.9 | Optional scheduled real test call | Owner's choice |
| R0.15, R4.15, R6.7, R9B.13, R10.12 | The live checks listed above, grouped by phase | Sessions A to D |
| L20 | Nooks | Only if the owner says Nooks is in use |

### 5.8 Release checks (R10.13, R10.14)

Run on the final candidate, after sessions A to D pass.

1. The draft's target commit is the commit CI passed, and CI is green on it.
2. The archive's SHA-256 matches `SHA256SUMS.txt`, and `codesign --verify --deep --strict` passes.
3. The installed app reports 2.0.0 and came up on the same workspace: leads, calls, numbers and keys intact.
4. The updater signature files (`.sig`) and `latest.json` are in the release, and `latest.json` says 2.0.0.
5. Rollback: the bundle moved aside at install starts again on the same data. Not yet checked: whether 1.0.10 opens a
   store that 2.0.0 has already migrated. Try that on a scratch copy of the store, never on the live one.
6. CI jobs match the "after the rebuild" table in section 12A of the plan.
7. Publish (section 7). Confirm the tag `v2.0.0` exists on the built commit and the release is marked Latest.
8. Updater path: a Mac on 1.0.10 is offered the published 2.0.0 and installs it with its data kept (the update needs
   one click). The owner's Mac already runs a hand-installed 2.0.0, so this needs the 1.0.10 bundle put back first
   (see 5) or a second Mac. This Mac is authorized for the signed update.

## 6. Owner decisions

Recommendation first in each.

1. **Go-ahead to fix I1, I2 and I8, then build and install candidate 5.** Recommended: yes. Nothing else can be
   signed off before it.
2. **Windows.** Recommended: ship 2.0.0 for Mac only and state it. The alternative holds the release for a PC and a
   signing route.
3. **How to run the opt-out and wrong-party checks (I4).** Recommended: a second phone the owner holds, typed into
   Test phone, used only for A11, D5 and D6. Second choice: run them on 203-821-1101 as the very last tests and
   accept that the line is done in this workspace. Not recommended: a code path that releases an opt-out, since
   "an opt-out always holds" is the rule that protects real prospects.
4. **Accept the test-line wording for L3, L4, L5 and L12 (I5).** Recommended: yes. The first half is shown live, the
   advance is shown by the browser suite, and L12 is 20 single test calls.
5. **Script proposals panel (I10).** Recommended: hide it for 2.0.0. It is untested live and works against the
   "simple, minimal" direction. The code stays.
6. **HubSpot.** Put the test contact in a segment (session D setup). Name the segment of real, eligible contacts to
   use first.
7. **Paid number search.** Yes or no on about $12 for the `a_plus` band, and the limit.
8. **RealValidito key and secret.** Without them the do-not-call check ships untested (K8).
9. **Lawyer check on auto-dial and transcription** (plan risks R1, R3, R12, R14). Not something a test can settle.
10. **Accept known limits K1 to K10 for 2.0.0.**
11. **Small ones:** OK for T1 ($0.044); the right outcome for the Greg Sarantopoulos call (I12); OK to close the 8
    test-line callback tasks (I13); whether Nooks is in use; trigger windows and strengths; whether the Jacob Pepper
    and Amir Ali calls reached the right person.

## 7. Commands

Check:

```bash
uv run python scripts/check.py
```

```bash
uv run python scripts/check.py full
```

Read the installed workspace without touching it. This copies the store and prints the heartbeat, the numbers, the
newest calls and the test-phone leads:

```bash
python3 - <<'EOF'
import json, shutil, sqlite3, tempfile
from pathlib import Path
live = Path.home() / "Library/Application Support/com.opendialer.desktop/local-3"
tmp = Path(tempfile.mkdtemp())
for n in ("opendialer.db", "opendialer.db-wal", "opendialer.db-shm"):
    if (live / n).exists():
        shutil.copy2(live / n, tmp / n)
db = sqlite3.connect(tmp / "opendialer.db")
for (data,) in db.execute("select data from users"):
    d = json.loads(data)
    presence = d.get("presence") or {}
    print("reachable:", d.get("reachable"), "| presence:", presence.get("status"), presence.get("at"))
for (data,) in db.execute("select data from number_inventory"):
    d = json.loads(data)
    print("number", d["phone"][-3:], d.get("lifecycle"), d.get("provider_status"), d.get("configuration_status"))
for (data,) in db.execute("select data from call_attempts order by created_at desc limit 5"):
    d = json.loads(data)
    print("call", d.get("state"), d.get("outcome"), d.get("provider_hangup_source"), d.get("machine_detected"),
          "test" if d.get("is_test") else "real", "attempt", d.get("attempt_ordinal"), "of", d.get("attempt_allowance"))
for (data,) in db.execute("select data from leads"):
    d = json.loads(data)
    if d.get("phone") == "+12038211101":
        print("test lead:", d.get("name"), d.get("source"), "attempts", d.get("attempt_count"), d.get("status"))
print("suppressions:", db.execute("select count(*) from suppressions").fetchone()[0])
shutil.rmtree(tmp)
EOF
```

Build a candidate, after a green full check:

```bash
git push origin main
```

```bash
gh run list --branch main -L 3
```

```bash
gh release delete v2.0.0 --yes
```

```bash
gh workflow run release.yml --ref main
```

Delete only the draft. Never delete a published release. Wait for CI to be green before starting the release run, and
for the release run to finish before installing.

Install a candidate on the owner's Mac (on the owner's request, with no call live). Work in a new empty folder:

```bash
gh release download v2.0.0 -R TanushV/OpenDialer -p "OpenDialer.app.tar.gz" -p "SHA256SUMS.txt"
```

```bash
grep " OpenDialer.app.tar.gz$" SHA256SUMS.txt | shasum -a 256 -c
```

```bash
mkdir x && tar -xzf OpenDialer.app.tar.gz -C x && codesign --verify --deep --strict x/OpenDialer.app
```

```bash
/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" x/OpenDialer.app/Contents/Info.plist
```

Then: quit the app (`osascript -e 'quit app "OpenDialer"'`), wait until no process under
`/Applications/OpenDialer.app/Contents/MacOS/` is left, move the old bundle aside (that is the rollback copy), copy
the new one in with `ditto x/OpenDialer.app /Applications/OpenDialer.app`, clear quarantine with
`xattr -dr com.apple.quarantine /Applications/OpenDialer.app`, start it with `open -g /Applications/OpenDialer.app`,
and wait for the backend (`opendialer-backend serve --port N`) to answer `http://127.0.0.1:N/health`. Never touch the
`local-3` data folder.

Publish, only when sections 5.2 to 5.5 and 5.8 are signed off:

```bash
gh release edit v2.0.0 --draft=false --latest
```

```bash
git fetch --tags && git rev-parse v2.0.0
```

Publishing the draft creates the tag on the draft's target commit. The second command confirms it.

## 8. Receipts

Every live session writes a receipt under `docs/evidence/rebuild/`: the check's number, the build, pass or fail, times
and notes. Use one dated file per day, in the style of
[2026-11-10-live-calls.md](evidence/rebuild/2026-11-10-live-calls.md). Then tick the matching items in the plan (R0.15,
R4.15, R5.13, R6.7, R9B.13, R10.12, R10.13, R10.14, R11.9) and add a dated entry to its Status section. A check that
was not run stays unticked, with the reason.
