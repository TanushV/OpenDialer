# OpenDialer rebuild plan (active)

Approved by the owner on November 6, 2026. This is the active plan. Read it in full on every continuation or context
compaction, including the Status section at the end, which is the current checkpoint.

It supersedes the "every feature stays" and "both modes" rules of [LEAN_PLAN.md](history/LEAN_PLAN.md), the manual-dial rule of
[CALLING_COMPLETION_PLAN.md](history/CALLING_COMPLETION_PLAN.md) and the "No DDGS" decision of
[CAMPAIGN_PREP_PLAN.md](history/CAMPAIGN_PREP_PLAN.md). Those files remain history for what a feature does.

## 1. TLDR

- Make OpenDialer a small dialer for Mac, Windows and Linux that is simple enough for bugs to show up in the code and tests.
- In 2.1, each rep has a desktop profile and a HubSpot segment. The existing relay shares sticky caller IDs across
  profiles and routes callbacks to the desktop that called (D33).
- Keep the working integrations: Telnyx, speech, ChatGPT agent, relay and signed releases.
- Cut about 60% of the code, then rebuild the call core, Dialer, Settings, StatML learning and research criteria.
- Work one module at a time. A calling module is finished only when live calls to the owner's test line pass and the
  owner signs off.
- Take leads from CSV or HubSpot through one intake path and log calls back to HubSpot. The CRM owns fit and the
  person; this app owns number accuracy. CRM leads get the number waterfall and a RealValidito do-not-call check inside the
  app; CSV leads are taken as they are.
- Build everything on `main`, with one worker in one thread. A local check loop gives answers in seconds while working and
  must pass in full before every push; GitHub CI confirms on a clean machine (section 12A).
- Estimate: backend 39,300 to about 17,100 lines, frontend 22,100 to about 6,600 lines, 25 to 33.5 working days, plus
  about 7 owner sittings of 30 to 60 minutes.

**What this plan can and cannot promise.** Failure-proof is not possible, because Telnyx, the network and the Mac can
always fail. Failure-resistant is possible: a failure stays contained, says what happened, recovers by itself where it
can and does no harm. Section 5 lists those guarantees and how each is tested.

## 2. Decisions

| # | Decision | Status |
|---|---|---|
| D1 | Remove hosted mode, PostgreSQL, teams and roles. One user, one Mac, one SQLite file. A team is several such Macs (D25). | Approved; clarified November 8 |
| D2 | Mac, Windows and Linux. The Windows build came out in R2.1 and returned in Phase 9D. The first Linux package targets Ubuntu 24.04 LTS x86-64. Each computer uses its own SQLite workspace and can join the shared pool. | Owner decisions, November 8 and November 13 |
| D3 | StatML stays and must actually learn. Repair it rather than cut it. | Owner decision |
| D4 | Coaching goes. The live helper stays, plus one AI after-call summary with a suggested outcome, because follow-up scripts read that summary. | Owner decision; the summary is an assumption the owner may veto |
| D5 | Auto-dial replaces manual-only dialing. One call at a time, with the rep present. | Owner decision; see risk R1 |
| D6 | Skip voicemail on detection, never before 7 seconds, unless the call is a final attempt or the rep presses Stay. | Approved default; see risk R2 |
| D7 | Keep transcript text on this Mac for 30 days, replacing the 5-minute lifetime. | Approved default; see risk R3 |
| D8 | ChatGPT agent stays primary. DeepSeek V4.1 Flash on OpenRouter with local DDGS search is the fallback. | Owner decision |
| D9 | Automatic recovery stays, trimmed. The manual recovery panel becomes a small Needs attention list. | Follows the owner's question |
| D10 | SSH tunnel, direct webhook route, API-key route and LiteLLM setting go. LiteLLM's code path is reused for the fallback. | Follows the owner's question |
| D11 | A new model goes live automatically once it beats current estimates on later calls, with a 10% comparison group and automatic rollback. Manual trials go. | Approved default; see risk R4 |
| D12 | Ship hotfix 1.0.2 first so calling works during the rebuild. | Approved default |
| D13 | Keep script memory, which feeds learned patterns to the agent. Manual script proposals and A/B tests go. | Assumption; the owner may veto |
| D14 | Build everything directly on `main`. No `rebuild` branch, no worktrees and no long-lived branches. Users only receive signed releases, so an unfinished `main` never reaches the installed app. | Owner decision, November 6; replaces the earlier `rebuild` branch default |
| D20 | One worker, in one thread. No subagents, Workflow runs, parallel sessions or concurrent writers at any point. See section 12A. | Owner decision, November 6 |
| D21 | CI/CD is part of the build. CI changes in the same phase as the code it covers, and a release can only be built from a commit whose CI passed. Publishing and installing stay manual. See section 12A. | Owner decision, November 6 |
| D22 | Local CI/CD is the working loop. One command, `scripts/check.py`, runs a fast tier on every commit, a full tier that covers everything GitHub CI covers before every push, and an unsigned local package build. Built first, in Phase 0. See section 12A. | Owner decision, November 7 |
| D24 | Phase 3 moves code by strangling, not in one pass. New packages on `Ctx` start now; each later phase writes its area as plain modules (calling in 4, learning in 8, leads and prep in 9, helper and after-call in 10) and deletes the mixin code it replaces. R3.2, R3.4, R3.5 and R3.6 complete with those phases. The migration, lead fields and package rules land in Phase 3. | Builder decision, November 7, under the owner's end-to-end instruction: a one-pass rewrite of 35,000 lines was the riskiest step in the plan and delivered nothing visible |
| D15 | The CRM stage owns fit and whether the person is still at the company. This app owns number accuracy. No `# Fit` section and no job-change check. | Owner decision |
| D16 | One intake path: CSV or HubSpot produces a row, the column map reads it, and everything after that ignores the source. | Owner approved |
| D17 | HubSpot: a list picks who is called and the app picks the order. The app writes native call records, opt-outs and `phone_status`. No universal writeback layer; HubSpot's call record is the shared format, so other dialers such as Nooks need no adapter. | Owner approved; creating CSV leads in HubSpot only on their first connected call is an assumption the owner may veto |
| D18 | Build the number check with Trestle through Treg. No paid live-line check and no paid namesake lookups. See section 9A. | Owner decision, November 6; this replaces the earlier test-rule result that kept the live-line check |
| D19 | When a CRM lead lacks a usable saved number, the app uses LeadOcean, AI Ark, Tomba, then Wiza and checks the results through Trestle. A usable CRM number goes directly to RealValidito screening. CSV leads skip the waterfall and screening. The outside runner stays for building CSV lists. Blitz joins first after its reset. | Owner decision, November 7, updated November 13 to use Treg only for missing numbers |
| D23 | Leads pulled from the CRM get a RealValidito do-not-call check (federal, state and TCPA litigator) before they can be dialed. Any hit is suppressed. CSV leads are not checked. | Owner decision, November 7; replaces the DoNotCallDNC key |
| D25 | Withdrawn by D27. Several reps call at once as seats. Each seat is one rep on one Mac with its own app, SQLite file, relay workspace, Telnyx Call Control application, telephony credential and API key, on the one Telnyx account. HubSpot's contact owner decides which seat calls a contact; a seat syncs only its owner's contacts, so no live lock is needed. One HubSpot Service Key, one Treg token and one RealValidito key serve every seat. A CSV file belongs to one seat. No app login: the Mac's own login protects a seat. See section 9C. | Owner decision, November 8; the Telnyx account is Verified (checked in the portal November 8) |
| D27 | One user only. Seats (D25), the shared number pool (D26), "Calls for", team calling, roles, passwords and sign-out are gone; every member of the chosen HubSpot list is synced whoever owns it. Section 9C and L21 to L24 are withdrawn. | Owner decision, November 10 ("We don't need this multi user") |
| D28 | Size caps stay warnings for 2.0. At 2.0 the backend is 32,537 lines (cap 18,000), the frontend 14,385, 126 routes and the largest file 1,557 lines (`main.py`). The excess sits in legacy modules that still work and are tested (D24); cutting them to the cap is a rewrite of working code for no behavior change, so it waits until a feature needs those modules. Bans and code rules stay hard failures. | Builder decision, November 10, under the owner's "finish to 2.0"; the owner can reverse it |
| D29 | A callback rings the app whenever it is open, on any page and paused or not, with a ring tone; otherwise the caller hears the greeting and a missed-call task is kept. Forwarding to a phone comes later (D30). | Owner, November 10 ("if someone calls back and the app is open it should show in the app") |
| D30 | Forwarding to the rep's phone is deferred; its field is hidden until it works with the app closed. | Owner, November 10 ("you can push off forwarding") |
| D31 | StatML is archived (tagged, then removed). The one learning view kept is stage drop-off: where answered calls end in the script. | Owner, November 10 ("archive statml"; "the main use it has is stage dropoff") |
| D32 | No voicemail feature: the skip at detection stays; the rep speaks into a voicemail by pressing Stay or on the last attempt. No recorded drop. | Owner, November 10 ("we just let the rep speak into the voicemail normally"; "keep skip") |
| D33 | In 2.1, reps share one Telnyx Call Control application, one SIP connection and a number pool. Each rep has a private computer profile, a WebRTC audio credential and one HubSpot segment. Each prospect keeps the same caller ID, and return calls ring the computer that placed the last confirmed call from the dialed number. The rolling 24-hour limit is at most 90 outbound attempts per number. Inbound callbacks use no outbound capacity. | Owner, November 11. Phase 12 specifies the rollout. |
| D34 | The rep starts personalized scripts for a chosen list in Leads. Importing contacts and saving the base script leave new generation idle. HubSpot number checks still run after sync. | Owner, November 13 |
| D26 | Withdrawn by D27. One shared number pool. Any seat can call from any pool number. The relay picks the number for each call (local area code first, then least used today) and counts daily use across all seats, so the per-number cap holds for the team. Pool numbers sit on one shared incoming route, and a callback rings the seat that last called that prospect, or goes to voicemail and a task on that seat. | Owner decision, November 8; that a Call Control dial may present any number on the account is confirmed in L23 |

Open owner inputs:

- [ ] Confirm the windows and strengths for the script's trigger list (needed by Phase 9; section 9 has defaults).
- [ ] HubSpot account, tier and Service Key (needed by Phase 9B).
- [ ] Whether Nooks is in use. Nothing is built for it either way; L20 applies only if it is.
- [x] Reverse lookups: not in the app and not in the waterfall. At about 30¢ a number the cost is not worth it (owner,
      November 6).
- [ ] Whether the two called rows with a Trestle no (Jacob Pepper, Amir Ali) reached the right person.
- [x] Blitz: no own key (owner, November 6). New lists run the D19 waterfall (section 9A).
- [ ] RealValidito API key and secret (D23). Its free 100 do-not-call credits cover the first test. CSV leads, including the
      current call sheet, stay unscreened by your decision (R14).
- [ ] Each paid waterfall run needs its own yes and limit. Remaining: 430 contacts, plus the 17 unresolved pilot contacts
      for Wiza. Treg's balance is $3.54, so a run of any size needs a top-up.
- [ ] OK to use the app's saved Telnyx key for T1 (section 9A).
- [ ] Correct outcome for the Greg Sarantopoulos call of November 6, currently saved as `no_answer` after 23 connected
      seconds.
- [ ] OK to close the open "Incoming callback" tasks that came from the owner's own test line (8 of 13 tasks have that
      callback number; 8 are open; confirm the list before closing).
- [ ] Lawyer check on auto-dial and transcription before real lists (R1, R3).
- [x] Telnyx account level: Verified, with every check met including KYC (portal, November 8). Docs give 10 calls at once.
- [x] Read the calls-at-once limit on the Verified card's "See privileges" page (R9C.1).
      Withdrawn November 10 by D27 (one user).
- [x] How contacts get an owner in HubSpot: by hand, bulk assign or a rotation workflow (rotation needs Sales Hub
      Professional). Every rep must be a HubSpot user to be an owner.
      Withdrawn November 10 by D27 (one user). Every member of the chosen list is synced.
- [x] Pool numbers to buy, about 3 to 5 per seat in the target area codes. The owner buys them; the app does not.
      Withdrawn November 10 by D27 (one user).
- [x] Research route on rep seats: each rep signs in to their own ChatGPT on their Mac, or rep seats use the OpenRouter
      fallback as their primary route (section 9C).
      Withdrawn November 10 by D27 (one user). The research route stays a Settings choice on this computer.
- [x] A second phone, or a second person, for the two-seat live check L22.
      Withdrawn November 10 by D27 (one user).
- [ ] A Windows PC for L25 to L27, and the Windows signing route: Azure Trusted Signing (monthly fee, needs a business
      identity check) or an OV code-signing certificate (R9D.4).
- [x] Number pool size for 3,000 dials a week: 15 local numbers (12 at the 50-a-day ceiling plus 3 spare), bought in
      the target area codes; full volume after about 6 business days of ramp (section 9C).
      Withdrawn November 10 by D27 (one user). Numbers stay this computer's own; NUMBER_CONNECT_PLAN.md still sizes them.

## 3. Audit evidence

Measured November 6, 2026 from source `730a80d`, the installed 1.0.1 workspace (read-only) and Supabase logs.

**System size**

| Item | Measure |
|---|---|
| Backend | 39,345 lines; 162 routes; 1,385 functions; 58 tables including SQLite's own statistics table, 14 of them empty |
| Frontend | 19,573 lines of TS/TSX plus 2,485 lines of CSS with 2 breakpoints and 60 `<details>` tags |
| Tests | 17,037 backend and 11,037 desktop lines |
| Backend structure | `Dialer`, `Application` and `Service` compose about 15 mixins into one object, so any code can reach any code |
| Largest functions | `solve` 331, `save_disposition` 301, `_help_context` 299, `compute` 268, `time_forecast` 264, `merge_semantic_state` 242, `parse_markdown` 218, `event` 214 |
| Dialer | 17 `useState`, 14 `useRef`, 11 `useEffect` |
| Real use | 7 calls, 1,090 `ml` jobs (1,076 are 10-minute `optimization_refresh`), 5,237 events |

**Root causes confirmed in code and data**

| Finding | Evidence |
|---|---|
| The audio leg dies at the end of every call. | 24 legs for 7 calls. Dial sets `park_after_unbridge: "self"` ([service.py:853](../services/backend/app/service.py)), which the Telnyx OpenAPI spec says parks the new prospect leg; an unparked leg hangs up. The frontend also hangs up the agent leg on purpose in `hangup()`, and `agent_event` pauses the rep on hangup. |
| The prospect may wait in silence if the rep's leg is lost. | Same parameter; `max_call_seconds` is 7200. |
| A prospect hang-up becomes a false transcription failure that blocks calling. | 4 of 4 `callee` hangups flagged, 0 of 3 `caller`. The owner's status showed `state: connected`, `speech_failed: true`, `connected: false`. Only Reconnect, setup import or a restart clears it. |
| The hang-up is not shown. | Events arrive in about 1 second, but no screen text says so; the dock says "Ready when you are". |
| Bad labels reach learning. | 4 of 7 training rows are test calls, and `delivery_test` is excluded only from connect metrics. Greg's mislabeled call is a training row. |
| StatML cannot learn much. | All 296 leads have segment `general`; title, industry and triggers are not features; goal `meeting_held` has 0 labels; `promotion_allowed` needs manual approval and a trial; `model_versions` has 0 rows. |
| Background work disturbs the UI. | 3,619 of 5,237 events are `optimization.updated` and `usage.updated`; `/v1/live` has no type filter, and `App.tsx` refreshes the whole workspace on them. The script editor also reloads on `optimization.updated`. |
| Research has no criteria. | "Anything that makes the call relevant." 147 of 292 scripts lost their written line in review; 27 of 243 leads with signal URLs cite one. |
| Settings reports the wrong thing. | "Done" means a Telnyx key exists. `carrier_call_verified` is hard-coded false. Five carrier fields hide once setup exists. |
| Layout fails in the real engine. | The app runs WebKit; the test config defaults to Chromium. At 1024x720 in WebKit the dock covers Save outcome. The toast covers Next call. A global `label` rule stacks checkboxes. |
| Dead code remains. | Voicemail-drop handling still sits in `event()`: 25 lines reference it, and nothing sets `voicemail_asset`. |
| Junk events fail as jobs. | 64 dead webhook jobs: unmatched events raise `RuntimeError` on purpose (intelligence.py:501). 49 are the rep's own SIP leg, and 45 share a session with an agent leg. |
| No crash containment. | No React error boundary. The sidecar exit handler has no restart path that I found. No keep-awake. No log file. About half of 45 `except Exception` blocks have no nearby log or raise. |
| The relay works but reconnects often. | In 24 hours: 3,474 successful exchanges, 21 Realtime reconnects, 23 failed exchanges. The failures I inspected followed an app restart. Any disconnect blocks new dials. |

**Checked and found fine**

- All 7 real dials completed, and hang-up events reach the app in about 1 second.
- Space after Go ready works, and the layout does not jump during a conversation.
- The dial and hang-up tracking is real protection, so it stays. It has never triggered on a real call.
- The saved master script passes its parser. The "6 things to fix" came from unsaved draft text; the editor must show
  that state clearly.
- The Gregory call was a real hang-up: `hangup_source: callee`, `normal_clearing`, speech on both sides until the end.

**Lead data**

Measured November 6, 2026 from the installed workspace (291 real rows) and the ledger of the phone waterfall that built
them (`~/Documents/Codex/2026-11-05/che/outputs/phone-waterfall`, outside this repo).

| Finding | Evidence |
|---|---|
| No number had been cross-checked before the test in section 9A. | The waterfall stops at the first hit: across 458 contacts with a number, a later finder was asked 0 times. Both run reports say `ownership_verified: 0`. |
| A live line does not prove the right person. | The HLR check says the line works, not whose it is. `ownership_status` is `unknown` on all 296 endpoints. |
| Blitz supplies most numbers at almost no cost. | 259 of 458 numbers for $0.13 in total, 94% live. All other finders: 199 numbers for $12.12. Accuracy is unmeasured for all of them. |
| Real calls cannot judge providers yet. | Only 3 calls have reached real rows, and one of those was a Blitz number. |
| Known-dead numbers stay eligible. | 3 rows have `network status: DEAD`; import ignores the column. |
| `fit evidence` is not evidence. | One identical sentence on all 291 rows; `qualification status` is `not_requalified_for_this_export` on all 291. |
| Row signals are usable but need rules. | A rough keyword pass finds 116 rows with a recent scripted trigger (81 hiring sellers, 20 funding, 15 expansion), 104 with signals outside the script, 47 with none and 24 too old. |
| Signal columns are not uniform. | URLs and dates split on a semicolon, summaries on a pipe. Splitting everything on a semicolon misaligns 98 rows. |
| Some signal pages cannot be read. | 28 of 40 sampled signal URLs opened with a plain fetch. |
| Import can silently lose columns. | `imported_row` keeps 60 columns and 8,000 bytes by dropping the last ones. Today's rows fit (56 columns, 3.9 KB); a HubSpot record might not. |

## 4. Target architecture

```text
Telnyx -> signed webhook -> Supabase relay -> carrier inbox -> apply_event() -> SQLite
                                                                     |
Rep keys -> API -> dial/hangup commands -> Telnyx             one state stream
                                                                     |
                                                     screen(state) -> fixed Dialer layout
```

**Shared calling (D33).** Each rep's computer runs this whole picture with its own SQLite file and HubSpot segment.
The relay tracks the pool, outbound usage per caller number, and the last confirmed caller for each prospect's sticky
number. Audio, transcripts and leads stay on the rep's computer.

**Backend layout.** Plain modules and functions that receive a small `Ctx` (db, clock, settings, carrier). Imports run
downward only.

| Package | Holds |
|---|---|
| `platform/` | db, jobs, config, secrets, logging, ctx |
| `providers/` | telnyx, telnyx_stt, codex, chat_agent (OpenRouter), ddgs, realvalidito, hubspot, treg, replay_carrier |
| `calling/` | state, dial, events, audio_leg, callbacks, recovery, relay, speech, blockers |
| `leads/` | intake, csv, hubspot_sync, waterfall, dnc, lists, suppressions, hours |
| `queue/` | selection, numbers |
| `numbers/` | sync, buy, configure, health |
| `learning/` | features, estimates, dataset, training, serving, promotion |
| `prep/` | agent, criteria, templates, research |
| `helper/` | live helper |
| `aftercall/` | summary, outcome, tasks, crm_log |
| `api/` | thin routes only |

**Ten code rules, each enforced automatically**

| # | Rule | Enforced by |
|---|---|---|
| 1 | One pure function handles every call state and event. | Generated state list shared with TypeScript; a `never` check breaks the build; a table test covers every state and event |
| 2 | One `blockers()` answers "can I dial?" with a code, a message and a fix. No other gate exists. | A test bans other refusal paths in dial and queue code |
| 3 | The screen is a pure function of one state stream. No call state in `useState`, no generation counters, no version checks. | Lint rule on the Dialer folder; exhaustive phase tests |
| 4 | Core records are typed Pydantic models with `extra="forbid"` over the document store. | Type check; a ban on `.data.get(` in core packages |
| 5 | Every failure is logged. One log file and a per-call timeline. | A lint test bans `except Exception` without a log or raise |
| 6 | The only fake is the carrier connection, replaying recordings of real calls. | A test bans `simulation` outside `providers/replay_carrier.py` and its factory |
| 7 | One user, one process, SQLite: no tenants, roles or edit versions. Durable dial and hang-up tracking stays. | Schema test; a ban on `tenant_id` |
| 8 | Background work cannot touch the call screen. | Only a named set of events reaches the UI during a call; tested |
| 9 | Eight UI components, three fixed regions, reserved space, no global element CSS. | Layout audit in WebKit for every phase at 1024x720 and 1440x940 |
| 10 | Size limits per file and overall. | `scripts/rules.py` fails CI above the limits in section 12 |

## 5. Failure guarantees

Each guarantee has an automated test where software can prove it, and a live check where only a real call can.

| # | Guarantee | How | Test | Live |
|---|---|---|---|---|
| G1 | A call can always be ended. | API hang-up and WebRTC leg end are independent. With parking removed, losing the rep's leg ends the prospect's. | Replay | L1, L9, L10 |
| G2 | A lead is never double-dialed. | Durable dial record, carrier command ID and read-only reconcile stay. | Crash-at-each-step test | L10 |
| G3 | The system never sticks. | Every state has a timeout and an exit. Shuffled event orders always reach a terminal state. | Property test on recorded calls | L12 |
| G4 | A blocked state names its reason and retries by itself. | `blockers()` and automatic retry. | Table test | Forced relay drop |
| G5 | An outage affects only what depends on it. | See table below. | Fault injection | L9, L13 |
| G6 | Nothing fails silently. | Log file, call timeline, error boundaries. | Lint and injected render crash | L13 |
| G7 | The backend comes back. | Sidecar auto-restart, then one recovery pass. | Kill test | L13 |
| G8 | The Mac stays awake while the rep is calling. | Hold an awake assertion while Ready, released on Pause. | Unit | L12 |
| G9 | Two seats never call the same contact, and an opt-out on one seat stops every seat. | A seat syncs only its owner's contacts; the last-contacted guard; opt-outs go to HubSpot at once and every sync adds them; sync before each session and every 30 minutes while calling. | Two-seat sync tests over recorded responses | L22 |
| G10 | Nothing that stops calling reaches the rep unannounced or without a way out. Every way calling can stop, in any area, either repairs itself within its promise or shows the one control that fixes it. | One calling model checked over every reachable state; a fault soak; a readiness self-test and watchdog in the app (Phase 9E) | Model check and soak in CI | L12, L13 |

| Outage | Behavior after the rebuild |
|---|---|
| Telnyx API | Dial fails with a plain reason; retry after confirmation; a live call can still end through WebRTC |
| Relay or Realtime | Realtime is only a speed-up. New dials stop only if HTTP delivery fails, which is also when no pool number can be leased. During a call, carrier status is polled and the WebRTC leg state is watched |
| Telnyx account call limit | The seat pauses with "Telnyx account call limit reached" and the fix; other seats keep calling |
| Transcription | Warning only; calling continues; the call is marked as missing speech |
| Live helper or OpenRouter | The script stays; calling continues |
| ChatGPT limit or login | Preparation falls back to V4.1 Flash; existing scripts still work |
| DDGS blocked | The agent uses row data only and marks the lead "no trigger found" |
| HubSpot | Sync and call logging pause and retry; calling continues on the last synced leads; failed writes reach Needs attention. A list not synced for 24 hours stops dialing its leads with a plain blocker, so a HubSpot opt-out is not missed for long; CSV leads are unaffected |
| Treg | Leads stay Unconfirmed and calling continues; the number check resumes when Treg is back |
| Mic removed | Call ends cleanly; blocker names the mic; Start calling returns when it is back |

## 6. Calling behavior

**Screen phases**

| Phase | Screen | Space | Other keys |
|---|---|---|---|
| Paused | Start calling, or the current blocker with its fix | Start | |
| Connecting | "Connecting audio" | Nothing | `P` pause |
| Dialing or ringing | Lead and script | Skip to next | `P` pause |
| Voicemail, skipping | "Voicemail: skipping" | Skip now | `S` stay |
| Voicemail, final attempt | "Leave your message" | Nothing | `Esc` hang up |
| Talking | Timer, script, helper | Nothing | `Esc` hang up, `M` mute |
| Ended | "They hung up · 0:23" or "You ended the call · 0:23", with a short sound and outcome buttons | Nothing | `1` to `6` save the outcome |
| Saved | 3-second countdown to the next call | Dial now | `P` pause, `C` change outcome |
| Skipped | No answer or busy saved; 1.5-second countdown | Dial now | `P` pause |
| Finished | "No one left to call right now" with the next eligible time | | |

Rules:

- One click or key saves an outcome. Everything else moves under Add detail.
- The screen shows "Call ended" as soon as the audio leg ends, then adds who hung up when the event arrives.
- Carrier facts (no answer, busy, failure) save immediately, without waiting for a job or an AI request.
- A suggested outcome is preselected, and the rep confirms it with one key.
- Auto-dial stops on Pause, a blocker, an incoming call, an unsynced opt-out or an empty queue.
- `wrong_party` is one of the six outcome keys. It stops dialing that number.
- A number with a `DEAD` live status is not dialed, and the lead says why.
- Start calling syncs the chosen HubSpot list first, with a short timeout. A failed sync warns and calling continues,
  under the 24-hour rule in section 5.

**Audio.** There is no Dial option to park the rep's leg, and a separate bridge after `call.answered` would add dead air
through the relay. So a fresh rep leg is created right after each call. The WebRTC client stays registered and only the
leg is renewed. Target: under 1.5 seconds, measured live.

**Rule-based order first.** Due callbacks, requested contact, final-stage pair, then StatML-ranked leads with first
calls ahead of retries. Calling hours, attempts and opt-outs stay hard rules that no model can change.

## 7. Settings

One page, nothing collapsed except Advanced. A row turns green only after a real check. The top of the page shows one
true status from `blockers()` with one fix button.

| Row | Where to get it | Powers | Check on save |
|---|---|---|---|
| Telnyx API key | [Telnyx API keys](https://portal.telnyx.com/#/api-keys) | Calls, numbers, transcription | Authenticated Telnyx request |
| Call connection | Automatic; import the setup file only when disconnected | Call events | Relay sign-in and one exchange |
| Phone numbers | Numbers page: buy, or add one you own | Caller ID | Route configured and ready |
| ChatGPT login | "Use this Mac's login" after `codex login` in Terminal, shown with a copy button | Research and scripts | Account status |
| OpenRouter key | [OpenRouter keys](https://openrouter.ai/settings/keys) | Live helper, summaries, fallback agent | Authenticated key check |
| Test phone | Type your own number | Test call button | Valid US number |
| Script | Script page | Every call | Parser passes |
| Calling hours | Edit in place | When calls are allowed | Valid hours |
| RealValidito key and secret (optional) | [RealValidito console](https://app.realvalidito.com) | Do-not-call check on CRM leads | `getcredits` request |
| HubSpot Service Key (optional) | HubSpot: Settings > Integrations > Service Keys | List sync and call logging | Authenticated request; required scopes present |
| Treg token (optional) | `treg org agent-new opendialer --cap <daily calls>` | Waterfall on CRM leads | Authenticated request |
| Calls for (with HubSpot) | Chosen from HubSpot's owners when the seat is set up | Which contacts this Mac syncs and calls | The owner exists in HubSpot; shown read-only, changed only by setting the seat up again |

Advanced: setup-file import, callback routing, greeting, caller name, backups, updates, Needs attention.

Removed from the screen: Voice API connection ID, webhook public key, webhook address, credential ID and SIP username
(they come with the setup file); Check connection JSON; Check readiness (status is always visible); SSH forwarding;
API-key and LiteLLM fields; publication reasons; budget and staffing forms.

The Telnyx link loads, but the portal is a single-page app, so confirm the exact screen in the build.

HubSpot stopped new legacy private apps on October 26, 2026 for existing accounts, so a Service Key is the route. I have
not tested one yet; R9B.7 confirms the exact scopes.

## 8. StatML

**Keep:** the staged funnel (human, right party, conversion), shrunken estimates, the immutable feature snapshots, the
separate gradient-boosted trainer, plain-JSON model serving, the 10% comparison group and script memory.

**Changes**

| Problem | Fix |
|---|---|
| Blind features | Add title seniority, industry, company size bucket, number source, trigger type, trigger strength, trigger age and list. Role fit only when the source supplies it as a mapped field (D15) |
| Dirty labels | Add `is_test` from the test-line allowlist and `delivery_test`; exclude from all learning; migrate the 4 existing test calls |
| Goal never labeled | Goal becomes `meeting_booked`, available at wrap-up; `meeting_held` stays a later correction |
| Models cannot go live | Automatic promotion using the existing out-of-time review (`reviewable`, no calibration regression, no coverage drop) |
| No safety after promotion | Roll back automatically if the comparison group beats the model by a set margin with enough calls |
| Churn | Recompute at most once a minute after a finalized call; train nightly when new labels exist; no UI events unless something changed |
| Invisible | Review shows a "What it has learned" card: calls used, best hours, best triggers, wrong-party rate by number source, current model, last change |

**Honest volume expectations.** Training starts at 100 labeled calls with at least 10 of each result. Pickup can be
learned first. Right-party learning needs a few hundred calls. Meeting prediction needs dozens of booked meetings. No
lift claim is valid without the comparison group having enough calls. Until then, estimates lean on priors.

## 9. Research and scripts

**Flow:** take in the row, map columns, read row signals, search the web, decide, write, check in code, then review the
facts.

1. **Column map.** CSV headers and HubSpot properties both become a row, and the same map reads it. Roles: title,
   headline, company description, industry, size, number source, live status, signal summaries, signal dates, signal
   URLs, LinkedIn activity. Each multi-value column has its own delimiter. If the counts do not line up, the signals stay
   one unaligned bundle and are never paired by position. Map first, then apply size limits, so a large record cannot
   lose a signal column. Show the map and allow corrections. Only mapped columns reach a model; email, provider IDs and
   bookkeeping columns do not.
2. **Row first.** The agent opens the row's signal URLs before searching.
3. **Criteria.** Rules live in a `# Triggers` section of the script, seeded from the master script's `# Before you dial`
   list.
4. **Fixed record for each lead.**

```json
{
  "trigger": {
    "type": "from the rule list, or none",
    "date": "YYYY-MM-DD or null",
    "source_url": "... or null",
    "quote": "exact text on that page, or null when evidence is row_only",
    "evidence": "page|row_only",
    "strength": "A|B|C|none",
    "from_row": true
  },
  "bridge": {"text": "one spoken line", "is_inference": true}
}
```

5. **Code checks.** The trigger type is on the list and the date is inside its window. For `page` evidence, the quote and
   the company are on the page. `row_only` evidence, for a signal whose page cannot be read, is capped at strength C and
   labeled "from the list, unverified" in the Dialer. These checks replace model judgment on those points.
6. **Review.** Facts are judged strictly. The bridge is judged as labeled inference, for tone and for contradiction with
   the facts, so it is not rejected merely for being an inference.
7. **Use.** The Dialer shows the trigger, strength and evidence link. Leads show A, B, C or none. Trigger fields feed
   StatML, and the scheduler can learn which triggers convert.

**Trigger list, from the master script's `# Before you dial`. Windows and strengths are defaults for the owner to confirm**

| Type | Window | Strength |
|---|---|---|
| Hiring AEs, SDRs or BDRs | 90 days | A |
| New sales leader | 180 days | A |
| New funding | 180 days | B |
| Expansion or new market | 180 days | B |
| Layoffs or a leaner team | 180 days | B |

**Fallback agent**

- Model `deepseek/deepseek-v4.1-flash` through OpenRouter's chat API, reusing the `InferenceClient` chat loop.
- Tools are the same as the primary agent, plus `web_search` (local DDGS) and `read_page` (existing `SafeFetcher`).
- Keep the no-retention route (`zdr`, `data_collection: deny`). Today's catalog shows Parasail serving it under that rule
  at $0.30/$1.20 per million tokens.
- Trigger: `PlanLimit`, login failure or repeated timeout on the ChatGPT route.
- Every record stores which route wrote it.
- Rough cost: 1 to 3 cents per lead, based on about 27,000 tokens per lead on the ChatGPT route. Measure it on 20 leads.
- `ddgs` depends on native `primp`, so packaging it in the signed sidecar is an explicit check.

## 9A. Number accuracy

Goal: know whether a number reaches the named person. Fit and the person's job are settled in the CRM (D15).

**Built whatever Treg testing shows**

- Import reads a mapped live-status column. `DEAD` is not dialed, and the lead says why.
- `wrong_party` stays one key and sets `ownership_status`. StatML's `endpoint_evidence` already reads that field.
- Number source is kept per lead, becomes a StatML feature, and the learning card shows wrong-party rate by source.

**Testing outside Treg**

| # | Test | Cost | What it settles |
|---|---|---:|---|
| T1 | Telnyx Caller Identity on the owner's line, after the owner OKs using the app's saved key | $0.044 | Whether it returns the owner's name, and at what confidence score |
| T2 | The same lookup on 20 real rows, only if T1 is useful and the owner approves sending them | $0.88 | Whether Telnyx can replace or back up Trestle |
| T3 | Wrong-party rate by number source on real calls, read after 100 connected calls | Call costs | The only real proof of accuracy. Providers can resell the same data, so agreement alone is not proof |
| T4 | Every distinct signal URL through the real fetcher | Free | The share usable as `page` evidence; replaces the 40-URL sample |
| T5 | Re-run the trigger dry run on the real rows after Phase 9 | Free | Trigger coverage against the baseline in section 3 |

**Treg test results, November 6, 2026**

63 real rows: 40 with numbers from Blitz, 20 from other finders and the 3 `DEAD` rows. Spend was $5.32 on Treg for this run,
matching the balance change. No call was placed. This is provider evidence, not proof from real calls.

| Check | Result |
|---|---|
| Known answers | Trestle was right on all 6: yes on the owner and on 2 prospects the owner confirmed reaching; no on a fake name and on 2 swapped name-and-number pairs |
| Trestle coverage | 59 of 60 live rows answered yes or no; 1 was blank |
| Trestle no rate | Blitz 4 of 40 (10%); other finders 4 of 19 (21%). The overall 8 of 59 means roughly 7% to 25% at this sample size |
| Reverse lookup | LeadOcean and ContactOut named the right LinkedIn profile on both known prospects. Neither has a record of the owner. Independent hit rate: 33 of 40 Blitz rows, 4 of 20 others, 0 of 3 `DEAD` |
| Trestle against reverse | First scored as agreement on all 37 rows where both answered. That overstates it: same-name results were counted as matches, and 3 of those rows (Seb Ginepio, Danielle Lacy, Hind Bachar) show the same name with a different job. Treat the agreement as name-level only |
| Wrong people found | 3 Blitz numbers, all live on the network, resolve to someone else: a colleague at the same company and 2 people at other companies |
| Second lookups | Blitz rows: 38 of 40 got a second number; 33 matched at least once; 5 only differed. Other rows: 8 of 20; 6 matched; 2 only differed |
| Different number against Trestle | Of the 7 rows where every second number differed, Trestle said yes on 4, no on 2 and blank on 1, so a different number alone is a weak signal |
| LeadOcean coverage | Returned a number on 37 of 40 Blitz rows. Its top pick matched Blitz's on 24, and 6 more listed Blitz's number lower down. It costs $0.005 per hit, against about $0.06 for the other paid finders together |
| `DEAD` rows | Trestle called all 3 valid, with activity 10, 30 and 94; 2 of the 3 got grade F |
| Blitz itself | Answered 0 calls. Treg's account is at Blitz's fair-use limit, with the next reset about November 29 |
| Not run | Telnyx (T1, T2), awaiting the owner's OK on the saved key |

**T4 and T5, November 8, 2026** (free; a scratchpad copy of the 296 real rows; no call placed)

| Test | Result |
|---|---|
| T4: every distinct signal URL through the app's fetcher | 153 URLs: 96 readable (63%), 4 too thin, 50 refused (LinkedIn 20, Adzuna 11, learn4good 7, PitchBook 5), 2 unsafe, 1 timeout. Refused pages can only be `row_only` evidence, capped at C |
| Column map on the real rows | Signals align on 210 rows. On 86 the URL column lists sources, not one link per signal, so those stay one unaligned bundle and are never paired by position |
| T5: a keyword proxy of the trigger rules on the row signals | 103 rows have a recent trigger on the list (68 hiring sellers, 22 expansion, 13 new funding), 141 have signals that match no rule, 52 have none, and 43 matches are too old or undated. The section 3 baseline was 116, by a looser pass. The agent's own decisions replace this proxy in R9.14 |

Agreed rules applied at the time, with the owner's later decisions:

- Build the Trestle check: **pass** (98% answered; known answers right).
- Add reverse lookups: passed the test rule, then **rejected** by the owner on cost per number.
- Keep Blitz: **keep** when it returns (no rate 11 points below the other finders, against a limit of 30 above). No own key.
- Drop the live-line check: failed the test rule because Trestle called the `DEAD` rows valid, then **dropped**. Trestle's
  actual rejection rule (no or grade F) rejected all 3 `DEAD` rows, and 0 of 83 numbers that passed Trestle were dead.

**No-Blitz waterfall pilot, November 6, 2026**

First 100 rows of the 530-row US leadership list. Finders: LeadOcean, then AI Ark, then Tomba. Each number went through the
free format check, Trestle, then the live-line check, and a rejected number moved on to the next one. The runner sits outside
this repo, in `phone-waterfall/sets/agite-us-leadership-noblitz-2026-11-06`. No call was placed.

| Measure | Result |
|---|---|
| Confirmed: name match and line not dead | 82 of 100 |
| Usable, but no name data | 1 |
| Only rejected numbers | 6 |
| No number found | 11 |
| First number rejected, later number accepted | 14 |
| Numbers that passed Trestle and were then dead | 0 of 83 |
| Spend | $5.19: 5.2¢ per contact, 6.3¢ per confirmed number, 13.6¢ for the most expensive contact |

This list is filtered to leaders located in the US, so it does not compare directly with the earlier 500. Scored by the
actual rejection rule (no or grade F), Trestle also rejected all 3 `DEAD` rows in the 63-row test. That is why the live-line
check was dropped, on a small sample; `wrong_party` and failed calls are the check on that decision.

**The waterfall (D19)**

Two homes, one set of rules:

- **CRM leads, inside the app.** A job after sync checks whether the lead needs a number and then runs the waterfall if needed.
  It ports the rules and the 20 offline tests from the outside runner.
- **CSV leads, outside the app.** `phone-waterfall/sets/agite-us-leadership-noblitz-2026-11-06/run_waterfall.py` builds the
  CSV. The app imports those rows as they are and runs no Trestle, finder or other check on them. It still reads a mapped
  `DEAD` column, which is free.

1. Use a locally validated CRM phone or mobile number and proceed to RealValidito screening. Run the waterfall if both are unusable or the selected number was marked wrong.
2. LeadOcean, then AI Ark, then Tomba. Never go past 10¢ on this stage.
3. Free format check. Malformed and non-US numbers are rejected without paying Trestle.
4. Trestle. Yes means use it and stop. Blank means use it marked Unconfirmed and stop. No, invalid or grade F means try the
   provider's second number, then the next provider.
5. Wiza last, outside the 10¢ stop, only if nothing passed. A miss is free. Its number goes through steps 3 and 4.
6. Nothing passes: no valid number, do not dial.
7. On the call, open with "Is this Liam at Brighterway?". A `wrong_party` mark stops that number. `mark-wrong` reopens the
   contact at step 2 and reuses answers already paid for.

| Per 100 contacts | Without Wiza (replayed on the pilot) | With Wiza (estimate) |
|---|---:|---:|
| Confirmed numbers | 80 | about 89 |
| Spend | $3.83 | about $5.36 |
| Cost per confirmed number | 4.8¢ | about 6¢ |

- The Wiza column applies its earlier results to the 19 contacts left over: 57 of 111 hits at 12.5¢ each, and 10 of 11 Trestle
  yes. It has not run on this list. At Treg's overall 17% hit rate the lift falls to about 3 numbers for about $0.50.
- Relaxing the stop to "start no new finder after 10¢" would add about 1 number per 100 for about 9¢. The agreed strict stop
  stays for now.
- Add Blitz first in the order after its reset, about November 29. It costs almost nothing and supplied 259 of 458 numbers.
- ContactOut (25¢) is the only finder worth testing against Wiza. It has never run as a finder on these lists.
- Known gap: a namesake passes. A Trestle yes only means someone with that name has that number. Liam Mahoney passed at grade A,
  and a Wiza review reports the same failure. Catching these needs a paid lookup per number, which the owner rejected, so the
  opening question and `wrong_party` are the catch. Revisit if `wrong_party` exceeds 5% of confirmed numbers after 100 connected
  calls.

**Number check inside the app (CRM leads only)**

- Applies only to leads pulled from the CRM. CSV leads skip it entirely and show "Not checked (CSV)".
- A mapped live-status column still applies to both sources: `DEAD` is not dialed. No live-line call is made.
- Each CRM lead runs the waterfall above once. A lead synced again is not rechecked unless its CRM number changed, or `mark-wrong`
  reopens it. Paid answers are stored by request fingerprint and never bought twice.
- Spend: a per-list limit you set before the first sync. The app shows the estimate (about 6¢ per contact, section 9A) and
  stops the job with a plain message at the limit. A separate Treg token caps daily calls.
- Trestle `true` is Confirmed and dials. `null` is Unconfirmed and dials with a label. `false` after every number is tried is
  Conflict: not auto-dialed, says why, dialable by hand. No passing number is "No valid number" and not dialed.
- A finder's number that passes is written back to the HubSpot contact's phone only if that field was empty, and
  `phone_status` is always written. A CRM number is never overwritten.

**Do-not-call check (D23, CRM leads only)**

- RealValidito `POST /dnclookup/validate`, up to 1,000 numbers per request, 1 credit each. It runs after the waterfall on the
  number that will be dialed, and before that lead can be dialed.
- `federal_dnc`, any `state_dnc` entry or `tcpa_litigator` adds a suppression with the reason, and the lead says which list.
  `invalid` marks the number Bad. `cleaned_number` passes.
- A result is good for 30 days; the federal rule requires a fresh scrub within 31. Leads are re-checked before then, at sync
  or at Start calling.
- No key, no credits (603, 604) or an outage leaves the lead "DNC not checked", and a CRM lead in that state is not
  auto-dialed. The blocker names the fix. This is the one check that blocks dialing, because a missed hit is a legal risk,
  not a wasted call.
- The suppression is also written to HubSpot as an opt-out reason, and a sync never clears it.
- Grade F replaces "activity 0" because no row scored 0. F flagged 2 of 3 `DEAD` rows and 1 live row that all 3 second lookups
  disagreed with. The sample is small, so T3 reviews this rule.
- A Trestle no is not proof of a wrong person. Only 3 of the 8 live rows flagged no had an independent reverse answer. T3
  measures how often a no is right.
- No token or a Treg outage leaves CRM leads with a CRM number Unconfirmed, so calling never blocks on it. A CRM lead with no
  number waits as "Needs a number".

## 9B. HubSpot

One account, barebones. HubSpot picks who is called and the app picks the order. Calling never depends on HubSpot. With
seats (D25), each Mac syncs only the list members owned by its "Calls for" user and writes its calls with that owner.

| Direction | What | When |
|---|---|---|
| HubSpot to app | Members of the chosen active list: mapped contact and company properties, contact ID, phone, opt-out property, `notes_last_contacted` | Sync button and Start calling |
| App to HubSpot | Native call record: timestamp, direction, status, outcome, duration, from and to numbers, note, contact association, `opendialer_call_id` | When the outcome is saved; the summary is added by a later update, so an AI failure never loses the call |
| App to HubSpot | `phone_status` on the contact: `wrong_number` or `dead`, plus `confirmed`, `unconfirmed` or `conflict` from the number check | When it changes |
| App to HubSpot | Opt-out | When it happens |

Rules:

- Add a lead by adding the contact to the HubSpot list. CSV import stays.
- A contact removed from the list stops being dialed and keeps its history. A contact with no number is not dialable and
  says why.
- A local opt-out always wins. A sync can add a suppression and can never remove one.
- CSV leads are created in HubSpot only on their first connected call, so cold rows do not flood the CRM.
- One outcome map. `no_answer` and a skipped voicemail become No answer; `busy` becomes Busy; a final-attempt voicemail
  becomes Left voicemail; `wrong_party` and `invalid_endpoint` become Wrong number. Anything where a person answered
  becomes Connected or a matching custom outcome such as Meeting Booked, read from the dispositions API at setup. An
  unmapped outcome shows at setup, not after a call.
- The summary goes to HubSpot; the transcript never does.
- Test-session and `delivery_test` calls are never written, except for contacts on the HubSpot test list.
- A failed write retries, then reaches Needs attention, and never blocks dialing. Before retrying an uncertain write, search
  for `opendialer_call_id`, because the HubSpot docs I found describe no duplicate guard.
- Other dialers such as Nooks write their own records, so there is no adapter. Exclusion lists built on HubSpot outcomes
  work across dialers. One guard: a contact any dialer reached inside the spacing window is not dialed, using
  `notes_last_contacted`. If L18 shows that property does not behave this way, the guard is dropped.
- Code: one `log_call(CallRecord)` and one outcome map, on one pinned dated API version, with no plugin system. Another
  CRM would be a second function.

Not yet confirmed, and settled in Phase 9B: Service Key scopes and which property holds do-not-call (R9B.7),
`notes_last_contacted` (L18), duplicate handling (L19), and Nooks outcome logging, where reports conflict (L20, only if
Nooks is in use).

## 9C. Seats and the shared number pool

**Withdrawn November 10 (D27): one user only.** The seat setup, pool, relay migration and "Calls for" code were removed
before the relay migration was ever applied. The text below is history.

How several reps call at once (D25, D26). There is no shared server and no in-app login.

**What is shared and what is per seat**

| Shared by every seat | Per seat |
|---|---|
| One Telnyx account (Verified), one outbound voice profile, one credential connection holding every rep's credential, one bill | A Telnyx API key named for the rep, so one can be revoked alone |
| The number pool, on one shared incoming route | A Call Control application with a channel cap of 2 (one call) |
| One HubSpot account and Service Key | A telephony credential for the rep's audio; Telnyx sends calls only to the last device registered on a credential |
| One Treg token (its daily cap limits total spend) and one RealValidito key | A relay workspace and its private setup file |
| The relay: number leases, daily counts per number, last caller per prospect | The Mac, its SQLite file, leads, learning and transcripts |

**Who calls whom.** HubSpot's contact owner, the standard HubSpot way to divide contacts. A seat's "Calls for" user is set
when the seat is set up and shown read-only after that, so a rep cannot switch to another rep's contacts by accident.
Reassigning the owner in HubSpot moves the contact on the next sync; its earlier calls stay on the old seat and are in
HubSpot. The last-contacted guard (section 9B) skips anyone another seat or dialer reached recently. A CSV file is
imported on one seat only.

**Opt-outs across seats.** An opt-out on any seat is written to HubSpot at once, and every seat's sync adds it and never
removes it. Each seat syncs at Start calling and every 30 minutes while calling, which narrows the 24-hour rule in
section 5. CSV leads have no shared record, which is why a CSV file stays on one seat.

**The number pool.**

- Before each dial, the seat asks the relay for a number. The relay picks a pool number in the prospect's area code if
  there is one, otherwise the nearest, then the one used least today, under the per-number daily cap counted across all
  seats. The lease ends with the call.
- The dial uses the seat's own Call Control application and presents the leased number. L23 confirms Telnyx accepts a
  number that is not on that application; if it does not, each seat keeps its own numbers and D26 is reversed.
- Every pool number is on one shared incoming route. A callback rings the seat that last called that number's prospect
  within 30 days. If that seat is offline or on a call, the caller reaches voicemail and that seat gets a missed-call task.
  A callback from someone no seat has called goes to the owner's seat.
- Number health is counted per number across the team, so the pool's health comes from everyone's calls.

**Pool size.** The ramp in `history/NUMBER_CONNECT_PLAN.md` ends at 50 attempts per number per day, under the 75 to 100 that
dialer vendors cite. 3,000 dials a week is 600 a business day, so 12 numbers at the ceiling, plus 3 spare for
resting or flagged numbers: 15. While new numbers ramp (15, then 25, then 40 a day), the pool gives about 225, 375 and
600 a day, so full volume arrives after about 6 business days. Add numbers by 1 per 50 daily dials.

**Concurrency.** Each live call is expected to use 2 channels, the prospect and the rep's leg; L22 measures it. Verified
gives 10, enough for 5 reps at once. Beyond that, ask Telnyx support. A channel-limit refusal pauses only that seat, with
a named blocker.

**Research on rep seats.** The ChatGPT route signs in per Mac and a ChatGPT login is personal, so each rep either signs
in to their own ChatGPT or their seat uses the OpenRouter fallback (section 9) as its main route. One OpenRouter key can
serve every seat.

**Setting up a seat.** The owner, once per rep:

1. Create the rep's Telnyx API key.
2. Run seat setup on the owner's Mac with that key. It creates the rep's Call Control application, telephony credential
   and relay workspace, and writes the rep's private setup file.
3. Import the setup file on the rep's Mac, sign the rep in to ChatGPT or use the fallback, and pick "Calls for".

**Learning.** StatML learns on each seat from that seat's calls. Pooling learning across seats would come from HubSpot's
call records later; it is not built now.

## 10. Live test protocol

Line: **203-821-1101**, the owner's own phone.

- A code allowlist limits test sessions to that number, on a test list excluded from metrics and learning.
- Sessions start only when the owner says go. No prospect is dialed by a test.
- The agent drives the app and the owner answers the phone. Ledger cost so far is about 1 cent per call.

| # | Scenario | Pass when |
|---|---|---|
| L1 | Owner answers, rep hangs up | "You ended the call"; outcome keys work; no Go ready |
| L2 | Owner answers, owner hangs up | "They hung up" within 2 seconds; no false flag; no block |
| L3 | Owner ignores the call | No answer saved; next call starts without a click |
| L4 | Owner declines | Busy or no answer saved; next call starts |
| L5 | Goes to voicemail | Skipped at detection, not before 7 seconds; next call starts |
| L6 | Final attempt reaches voicemail | Line stays open; rep can speak and hang up |
| L7 | Opt-out during a call | Call ends; number suppressed; never redialed |
| L8 | Owner calls the number back | Rings in the app; answer works; missed call makes a task |
| L9 | Wi-Fi off mid-call | Owner's phone call ends; app says what happened; recovers when Wi-Fi returns |
| L10 | App force-quit mid-call | Owner's phone call ends; reopening shows the call owed an outcome; no second dial |
| L11 | App in background when audio connects | Audio connects, or the blocker says what to do |
| L12 | 20 calls in a row | No Go ready, no stuck state, audio both ways on a sample, Mac stays awake |
| L13 | Backend killed mid-call | Backend restarts; call can be ended; screen stays usable |
| L14 | Owner's test contact on a HubSpot test list, then Sync | Lead appears with the right name, number and mapped signal columns; syncing again changes nothing |
| L15 | Call that lead and save an outcome | A call record appears on the contact within a minute with the right outcome, duration, direction and summary, and no transcript |
| L16 | Save `wrong_party` | HubSpot shows Wrong number and `phone_status`; the number is not redialed; the next sync does not restore it |
| L17 | Opt out in HubSpot and sync; opt out in the app and sync | Both suppress; neither sync clears it; HubSpot shows the app's opt-out |
| L18 | Log a call by hand in HubSpot on the test contact, then Sync | `notes_last_contacted` moves and the app skips the contact; if not, the guard is dropped |
| L19 | Cut the network right after a call record is posted | Exactly one call record after the retry |
| L20 | Only if Nooks is in use: one Nooks call on a test contact | Its outcome appears in HubSpot and the shared exclusion list removes the contact |
| L21 | Set up a second seat from its setup file | It shows its own owner, syncs only that owner's test contact and never the first seat's |
| L22 | Two seats call two test phones at once | Both calls have audio both ways; neither seat sees the other's contact; the channels one call uses are recorded |
| L23 | A seat dials from a pool number that is not on its own application | The test phone shows the pool number; if Telnyx refuses it, D26 is reversed |
| L24 | Call the pool number back from the test phone | It rings the seat that called; with that seat paused it reaches voicemail and makes a task there |
| L25 | Install the signed Windows build on a clean PC and set up a seat | Setup, keys and blockers match the Mac |
| L26 | Windows seat calls 203-821-1101 while a Mac seat calls the second phone | Audio both ways on both; separate pool numbers; no shared contact |
| L27 | Update the Windows seat, then roll back | Workspace data, keys and call history are kept |

Each session records a receipt under `docs/evidence/rebuild/`: scenario, version, pass or fail, timing and notes. It also
answers whether the hourly carrier cap applies and whether the automated notice plays.

Use the Phase 12 pilot for the D33 shared application. L23 describes the earlier design with a Call Control application
per rep.

## 11. File dispositions

Targets are hand allocations from the function inventory, not guarantees. Section 12 holds the caps.

**Backend**

| File | Today | Action | Target |
|---|---:|---|---:|
| `main.py` | 1,838 | Thin routes in `api/`; about 95 routes go | 600 |
| `service.py` | 1,257 | Dial, hang-up and import move out; `seed`, budgets and scoping go | 500 |
| `intelligence.py` | 1,406 | Job dispatch and packet; review machinery goes | 300 |
| `rep_workflows.py` | 1,251 | Outcome and tasks; `save_disposition` under 120 lines | 500 |
| `administration.py` | 1,052 | `blockers()` and backups; capability matrix and ingress check go | 300 |
| `number_operations.py` | 993 | Sync, buy, configure; reassign and review go | 550 |
| `privacy.py` | 798 | Raw-speech store goes; transcripts and redaction stay | 200 |
| `db.py`, `operational_schema.py` | 1,035 | No tenants; fewer tables and indexes | 600 |
| `managed_webhooks.py` | 382 | Gate on HTTP delivery; drop `speech_failed` | 280 |
| `workspace.py` | 368 | One snapshot | 150 |
| `measurement.py` | 580 | Call timing and connect counts | 60 |
| `reporting.py` | 328 | Delete | 0 |
| `billing.py`, `accounting.py`, `model_tokens.py` | 986 | Simple usage totals | 190 |
| `lead_lists.py` | 313 | Keep, simplified | 220 |
| `cli.py`, `security.py`, `secret_files.py`, `config.py`, `runtime.py`, `priority.py` | 814 | Local token and credential encryption stay; password login and sessions go | 533 |
| `grounding.py`, `transcripts.py`, `live_transcription.py` | 349 | Keep, simplified | 296 |
| `optimization/capacity.py` | 1,414 | Delete; a per-number daily cap remains | 0 |
| `optimization/projections.py` | 1,372 | Keep estimate statistics only | 250 |
| `optimization/workflows.py` | 1,429 | Dataset, training and refresh | 420 |
| `optimization/learning.py` | 674 | Features, rows, score, comparison group | 330 |
| `optimization/api.py` | 1,127 | Learning status only | 60 |
| `optimization/script_lab.py` | 790 | Memory and script import | 200 |
| `optimization/script_learning.py` | 487 | Report and memory digest | 300 |
| `optimization/experiments.py` | 502 | Comparison-group assignment | 40 |
| `optimization/numbering.py` | 473 | Selection, warmup, health | 300 |
| `optimization/conversations.py` | 357 | Prior-call features | 100 |
| `optimization/geography.py` | 199 | Keep | 199 |
| `optimization/goals.py` | 83 | Delete | 0 |
| `ml/` | 1,143 | Keep; add automatic promotion and rollback | 1,035 |
| `scheduling/` | 1,099 | Remove multi-rep holds and comparisons | 773 |
| `coaching/workflows.py`, `context.py` | 2,223 | Live helper only | 750 |
| `coaching/engine.py`, `requests.py`, `conversation.py`, `contracts.py`, `sources.py`, `knowledge.py` | 852 | Keep, simplified | 579 |
| `coaching/library.py`, `replay.py` | 561 | Delete | 0 |
| `postcall/` | 1,467 | One summary and one outcome suggestion; rubrics, review and date parsing go | 280 |
| `compliance/operating.py`, `screening.py` | 1,005 | Calling hours and manual do-not-call check | 270 |
| `compliance/registry.py`, `consent.py` | 492 | Delete | 0 |
| `compliance/suppressions.py`, `policy.py`, `phone.py` | 311 | Keep | 247 |
| `preparation/` | 2,195 | Add criteria; simplify state | 1,600 |
| `research/fetch.py` | 143 | Keep | 143 |
| `providers/` | 2,002 | Add `ddgs`; `simulated` becomes replay carrier; API and LiteLLM modes go | 1,759 |
| `telephony/` | 2,872 | Dead voicemail goes; inbound simplified; recovery trimmed | 1,430 |
| `jobs/queue.py` | 322 | Fewer states; keep crash recovery | 200 |
| New: `providers/hubspot.py`, `leads/intake.py`, `leads/hubspot_sync.py`, `aftercall/crm_log.py` | 0 | One intake path and HubSpot | 470 |
| New: `providers/treg.py`, `leads/waterfall.py`, `providers/realvalidito.py`, `leads/dnc.py` | 0 | In-app waterfall and do-not-call check on CRM leads; replaces the DoNotCallDNC path | 450 |
| New: `numbers/pool.py`, seat setup in `numbers/configure.py`; relay migration and function for leases, counts and callback routing | 0 | Seats and the shared pool (section 9C) | 350 |
| **Total** | **39,345** | | **about 17,100** |

**Frontend**

| Item | Today | Target |
|---|---:|---:|
| `App.tsx`, `types.ts`, `api/*`, `context.tsx` | 2,187 | 1,033 |
| Dialer: `DialerPage`, new `screen.ts`, `CallControls`, `WrapUp`, `LiveCoach`, `ProspectBrief`, `ScriptReader`, `Keypad`, `InboundCall`, `telephony/*` | 3,483 | 1,758 |
| Leads and lists: Leads part of `Operations`, `LeadLists`, `PreparationPanel`, column map, HubSpot list picker | about 1,600 | 900 |
| Numbers: Numbers part of `Operations`, `NumberPurchasing` | about 1,800 | 500 |
| Script: `ScriptEditor`, `ScriptMemory`, script part of `Operations` | about 990 | 390 |
| Review: `ReviewPage`, `FollowupTasks`, new learning card | 917 | 450 |
| Settings: `SettingsPage`, `CodexLogin`, `ManagedConnection`, `CallbackRouting`, `CredentialField`, Needs attention, HubSpot row | 2,406 | 620 |
| UI kit (new), remaining `state/*` | about 420 | 500 |
| CSS | 2,485 | 600 |
| Delete: `TrialControls`, `OptimizationPanel`, `BillingReconciliation`, `Staffing`, `WorkTime`, `TokenUsage`, `Rubrics`, `ScoreReview`, `ConversationLearning`, `ConversationObservations`, `AdvancedIntelligence`, `Coaching`, `ConversionForm`, `DncScreeningPanel`, `OperatingPolicySettings`, `RecoveryPanel`, `AdminWorkflows`, `ProductKnowledge`, `HistoryMore`, `useWorkHeartbeat`, `worktime`, `definitions`, `useSpeechExpiry`, `state/privacy` | about 5,800 | 0 |

**Tests**

| Group | Action |
|---|---|
| Keep and adapt | `test_telnyx_contracts`, `test_telnyx_stt`, `test_managed_webhooks`, `test_prep_threads`, `test_codex_login`, `test_codex_native`, `test_research_*`, `test_script_markdown`, `test_script_playbook`, `test_policy`, `test_final_stage`, `test_pure_serving`, `test_training_isolation`, `test_adaptive_model`, `test_openrouter_coach`, `test_coach_stream`, `test_live_conversation_context`, `test_number_provider_paths`, `test_number_warmup`, `test_state_number_allocation`, `test_secret_storage`, `test_sqlite_plans`, `test_desktop_launch`, `test_providers` |
| Trim | `test_grounding_review`, `test_script_learning`, `test_optimization_numbers`, `test_remediation_semantics`, `test_calling_readiness`, `test_store_format`, `test_action_tools` |
| Delete | `test_unified_planning`, `test_time_capacity`, `test_planner_correctness`, `test_capacity_conservation`, `test_adaptive_capacity`, `test_coach_replay`, `test_strategy_policy`, `test_conversation_associations`, `test_coach_context_interpretation`, `test_conversation_timing`, `test_number_concurrency`, `tests/test_local_ingress_proxy` |
| Acceptance | Keep calling, learning, preparation, campaign prep, follow-up scripts, live conversation, live helper, number setup and purchase, lists, auto wrap-up, playbook. Trim compliance, workspace, models, operations, post-call and script learning. Delete list number plan and connect report |
| Browser | Rewrite on the new screens as six specs: calling, settings, leads, numbers, script, review. Delete specs for removed features. Run in WebKit |
| New | State table, screen phase table, recorded-call replay, shuffled-order property test, crash at each dial step, layout audit, lint bans, intake contract, signal parsing, HubSpot contract tests over recorded responses, HubSpot fault injection |

**Repository**

- Delete `deploy/` (Compose, Caddy, Dockerfile, hosted runbooks), `scripts/disposable_database.py`,
  `scripts/evaluate_coach.py`, `packages/evaluation-fixtures` and the PostgreSQL and Windows CI paths.
- Keep `supabase/`, `scripts/package_macos.py`, `build_sidecar.py`, `backend_entry.py`, `trainer_entry.py`,
  `release_manifest.py`, `size.py`, `e2e_server.py`, `tools/artifact-verifier`.
- Remove `psycopg`, `pwdlib` and `PyJWT` if nothing still needs them. Add `ddgs`. Keep `numpy`, `scikit-learn` and
  `threadpoolctl` for the trainer.
- Move finished plans to `docs/history/`. Keep README, GETTING_STARTED, ARCHITECTURE (rewritten), SCRIPT_PLAYBOOK,
  RELEASE_NOTES, this plan and `evidence/`. `history/NUMBER_CONNECT_PLAN.md` stays open for its traffic ramp and measurement
  gates, resumed after Phase 10.

## 12. Budgets and estimates

| Measure | Today | Target | CI cap |
|---|---:|---:|---:|
| Backend lines | 39,345 | about 17,100 ± 2,000 | 18,000 |
| Frontend lines including CSS | 22,058 | about 6,600 ± 1,000 | 7,500 |
| Tests | 28,074 | 8,000 to 10,000 | 12,000 |
| Tables | 58 | about 25 | 30 |
| Routes | 162 | 60 to 70 | 75 |
| Largest function | 331 | 80 | 120, with a short named exception list |
| Largest file | 2,190 | 500 | 700 |

How firm: a name-based trace from the kept routes gives an upper bound of about 24,000 backend lines before any
simplification. The 17,100 comes from allocating each function by hand, plus about 580 new lines for intake, HubSpot and
the number check.
Real numbers are known at the end of Phase 3.

| Phase | Work | Days |
|---|---|---:|
| 0 | Local check loop, then hotfix 1.0.2 | 0.75 to 1.25 |
| 1 | Guardrails and replay | 1 |
| 2 | Delete the cut list | 1.5 to 2 |
| 3 | Split the object, type the core, migrate | 2 to 3 |
| 4 | Call engine and guarantees | 2 to 3 |
| 5 | UI kit and Dialer | 1.5 to 2 |
| 6 | Auto-dial and voicemail | 1 |
| 7 | Settings | 1 |
| 8 | StatML | 1.5 to 2 |
| 9 | Research, intake, number check and fallback | 3 to 4 |
| 9B | HubSpot, in-app waterfall and do-not-call | 2 to 3 |
| 9C | Seats and the shared number pool | 2.5 to 3.5 |
| 9D | Windows build, signing and updater | 3 to 5 |
| 10 | Helper, summary, remaining pages, docs, 2.0 | 1.5 to 2 |
| | **Total** | **25 to 33.5** |

Basis: this repo's own history, including 58,000 lines removed on October 22 and 8 lean-plan steps in about 2 days. Phase
4 is the least predictable, because each live failure adds time. The intake and HubSpot days are a rougher estimate than
the rest, because they come from reading API docs and not from a function-level audit.

## 12A. How it is built

**One worker, in one thread (D20)**

- All reading, editing, testing and verifying happen in one working thread, one step at a time, in the order of section 13.
- No subagents, Workflow runs, parallel sessions, worktrees or concurrent writers. This holds even when work looks mechanical
  or parallel, and it overrides any tool or skill default that suggests delegating.
- If another model or session ever takes over, that is a sequential handoff: the previous one has stopped, and its claims are
  checked against receipts before work continues.
- Parallel test execution such as `pytest -n auto` and parallel CI jobs are not workers, so they stay.

**Everything on `main` (D14)**

- Every step is committed directly to `main` and pushed, with no feature branches or pull requests.
- Each commit keeps the app able to start and call. A step too large for that is split.
- If the newest commit's CI fails, fixing it is the next step. No new work starts on a red `main`.
- A phase is finished only when CI has passed on its exact last commit.
- Each signed release has a tag. `v1.0.2` is the rollback point, and a bad commit is reverted rather than rewritten. There is no
  force-push.
- An urgent fix during the rebuild lands on `main` and ships when `main` is next green and has passed the live checks for what
  changed. If `main` cannot ship in time, the owner decides what to do (R10).
- Migration is the main exposure: installed data stays on `local-2` until a release containing Phase 3 is installed, which needs
  the owner's OK and passes R3.8 first.

**Local CI/CD (D22)**

Local checks are the working loop. GitHub CI is the second opinion on a clean machine and the gate for releases. One command
runs every tier, so a local pass and a CI pass mean the same thing.

| Tier | Command | Runs | Target | When |
|---|---|---|---|---|
| Fast | `uv run python scripts/check.py` | Ruff lint and format, TypeScript, Prettier, Vitest, size caps and code-rule bans, and the backend tests not marked `slow` | Under 20 seconds | After each change; the pre-commit hook |
| Full | `check.py full` | Everything in Verify: all backend tests, the browser suite in WebKit on SQLite with parallel workers (from R1.2a), the layout audit, and the Rust format, test and Clippy checks | Under 5 minutes | Before every push; the pre-push hook |
| Package | `check.py package` | Unsigned, ad-hoc-signed Mac build with both sidecars, then a smoke start that reaches `/health` and stops, against a temporary data folder | Measured in R0.0 | When packaging, sidecars or dependencies change, and before asking for a signed release |

- `check.py <path or keyword>` narrows the fast tier to matching tests while working. A narrowed run never replaces the full
  tier before a push.
- A test that takes over 1 second on this Mac is marked `slow`, and an unmarked test over 5 seconds fails the run
  (`OPENDIALER_SLOW_LIMIT`, with room for a loaded machine). The fast tier stays fast without anyone having to remember.
- The hooks live in `.githooks/` and are turned on with `check.py install-hooks`. Skipping them with `--no-verify` is not
  allowed on this project.
- The Verify workflow calls the same tiers, so the two cannot drift. A failure on GitHub that passed locally is a defect in
  the local check, and fixing it is the next step.
- Local checks never use the installed app's data, credentials or network services, and they place no calls. The package
  tier never installs, never touches `/Applications` and never runs the updater.
- Speed fixes live in tests, not in the rules. If a tier misses its target, the slowest tests get faster or more parallel.
  Checks are never dropped to save time.

Measured on this Mac, November 7, 2026, from `730a80d`. The Mac is shared, so these are rough numbers:

| Check | Time | Note |
|---|---:|---|
| Ruff lint and format | under 1 second | |
| TypeScript, Prettier, Vitest | about 7 seconds together | |
| Backend, 883 tests on 10 cores | 111 seconds | Wall time is set by a few long tests: script learning 33, challenger training 33 and serving parity 29 seconds |
| Browser suite | Cannot run locally | The config always starts the hosted server, which requires a PostgreSQL address. The only browser run is CI's, at about 21 minutes with one worker |
| `check.py` fast, after R0.0 | 39 seconds at load average 22 | 820 backend tests run; 59 marked `slow` are left out. Backend startup across workers is most of it |
| `check.py full`, after R0.0 | 92 seconds at load average 31 | Backend and Rust run side by side; browser suite still CI only |
| `check.py full`, after R1.2a | About 2 minutes, then the browser suite alone in about 4.5 | Running the browser suite beside the backend starved both: 281 seconds for the backend and load-caused failures, so it runs second. Misses the 5-minute target on this shared Mac |
| Browser suite without hosted specs | 26 minutes, one worker | 81 passed, 40 failed: 7 still reach the hosted server through `tests/simulation.ts`, and most of the rest timed out on a loaded Mac. The same commit passes in CI, so the suite is not reliable outside CI today |

**CI/CD today, measured November 6, 2026**

| Item | State |
|---|---|
| `ci.yml` (Verify OpenDialer) | Runs on every push to `main`: lint, backend on SQLite and PostgreSQL, frontend, browser tests and Mac shell. About 21 minutes, nearly all of it browser tests. The latest run passed |
| `release.yml` | Manual start. Builds signed Mac and Windows artifacts and creates a draft release |
| Gap: release is not tied to CI | `ee90345` and `c5e9da5` both failed Verify, yet signed candidates were built from them. They were draft candidates, but nothing in the workflow prevents this |
| Gap: wrong browser | CI tests in Chromium, and the app runs in WebKit, which is why the layout failures in section 3 passed |
| Gap: rules not enforced | `scripts/size.py` and the code-rule bans do not run in CI |
| Gap: stale paths | PostgreSQL, Windows and `deploy/backup` are built into both workflows, and `release_manifest.py` refuses a release without Windows artifacts |
| Gap: no local loop | No check script, no hooks and no local browser run. Every full answer waits for a push and 21 minutes |
| No branch protection | Left off on purpose: direct pushes are the workflow, and CI failures stop work by the rule above |

**CI/CD after the rebuild (D21)**

| Stage | What runs | When |
|---|---|---|
| Local | `check.py` fast and full, plus package when needed | Every commit and every push, on this Mac |
| Verify | `check.py full` plus both sidecars starting up: lint, types, size caps, code-rule bans, backend on SQLite, frontend, replay and property tests, HubSpot contract tests, WebKit browser and layout audit, Mac shell | Every push to `main` |
| Release | Check that Verify passed on this exact commit, then build signed Mac, Windows and Linux artifacts and a draft release | Manual start, on the owner's OK |
| Publish | Owner approves the draft | Manual |
| Install | Signed updater on this Mac | Manual, only on an explicit owner request |

- No automatic publishing and no automatic install.
- CI never holds carrier, HubSpot, Treg, OpenRouter or ChatGPT credentials, and it places no calls. It uses recorded responses
  only. Passing CI is software evidence, and the live checks in section 10 stay separate gates.

## 13. Phases and todos

Rules for every phase:

- Tests come first. Every defect gets a failing regression before the fix.
- Work follows section 12A: one worker, in one thread, on `main`.
- Run `check.py` (fast) after each change and `check.py full` before each push. Never push a failing full check.
- A phase ends only when CI passes on its last commit.
- Commit coherent steps often, with no AI attribution.
- Installing on this Mac needs an explicit owner OK.
- Record progress in the Status section.

### Phase 0: hotfix 1.0.2

Goal: calling works without false blocks while the rebuild runs.

Local loop first (section 12A):

- [x] R0.0 Add `scripts/check.py` with the fast, full and package tiers, plus the `.githooks` pre-commit and pre-push hooks
      and `check.py install-hooks`. Until R1.2a, the full tier leaves out the browser suite and says so, and that suite stays in
      CI only. Mark tests over 1 second `slow`. Point the Verify jobs at the same commands. Record each
      tier's time on this Mac as the first baseline.

Tests first:

- [x] R0.1 Capture sanitized carrier timelines from the 7 real calls as test fixtures, with no names, numbers or speech.
- [x] R0.2 Failing regression: a prospect hang-up where audio ends before the hangup event produces no gap and no block.
- [x] R0.3 Failing regression: an ended call leaves the rep able to dial again after audio reconnects.
- [x] R0.4 Failing regression: an unmatched event from the rep's own SIP leg finishes without a failed job.

Fixes:

- [x] R0.5 `managed_webhooks.py`: remove `speech_failed` from `connected`; expose it as a warning.
- [x] R0.6 `managed_webhooks.py`: give each refusal its real cause (sign-in needed, delivery gap, reconnecting).
- [x] R0.7 `DialerPage.tsx`: wait a short grace period and re-read the call before reporting an audio gap;
      `live_transcription.py`: ignore a gap report for an ended call.
- [x] R0.8 `service.py`: remove `park_after_unbridge`.
- [x] R0.9 `client.ts`, `DialerPage.tsx`, `workflows.py` `agent_event`: an ended leg after a call reconnects audio and
      keeps the rep ready; only Pause pauses.
- [x] R0.10 Dialer: show "They hung up · m:ss" or "You ended the call · m:ss"; add `provider_hangup_source` to the type
      and confirm the API returns it.
- [x] R0.11 `ingress.py`, `intelligence.py`: unmatched events without our client state become `ignored`, logged once.
- [x] R0.12 CSS: the dock no longer covers Save outcome; the toast no longer covers Next call.
- [x] R0.13 Add a top-level React error boundary and a backend log file.

Ship:

- [x] R0.13a `release.yml`: refuse to build unless Verify passed on the exact commit, with a test of the check.
- [x] R0.14 Full CI, then signed 1.0.2 on the owner's OK. Tag `v1.0.2` as the rollback point.
      Superseded: 1.0.3 to 1.0.10 shipped through the gated release and 1.0.10 is installed; each release tag is a rollback point.
- [ ] R0.15 Live L1, L2, L3, L9 and one more answered call; receipt under `docs/evidence/rebuild/`. L9 confirms that
      removing the parking parameter ends the prospect's leg when the rep's is lost.

Done when: 5 live calls need no Go ready and produce no false block. **Owner sitting 1.**

### Phase 1: guardrails and CI

Goal: today's failures become failing tests before any structure changes.

- [x] R1.1 Confirm `main` is at the `v1.0.2` tag with CI passing, and record the CI time as the baseline.
      Superseded with R0.14: CI timing baselines are recorded in section 12A and `docs/evidence/rebuild/`.
- [x] R1.2 Add a WebKit Playwright project and make it the default, locally and in CI. Move the CI browser job to WebKit on
      `macos-15`, because the app only runs on Mac. The repository is public, so this adds no CI cost.
- [x] R1.2a Make the browser suite pass locally: parallel workers, each with its own server and SQLite file; the hosted server
      and the hosted calls in `tests/simulation.ts` only when a PostgreSQL address is set; timeouts that hold on a loaded Mac.
      Then add it to the local full tier. Meet the full-tier target in section 12A, or record why not.
      Done November 7: 169 pass and 16 hosted-only skip in 4.1 to 4.6 minutes with 4 workers at load 16 to 21. Runs that
      fail under that load pass alone, so the local run retries once and reports a retried test as flaky; CI does not
      retry. The run found two real WebKit bugs, now fixed: a reload undid the rep's dialing-list choice, and dates read
      differently by engine. Hosted mode stays on a Linux CI job until R2.1.
- [x] R1.3 Add the layout audit: for every Dialer phase at 1024x720 and 1440x940, the primary action is visible and not
      covered, no sideways scroll, no clipping, and layout shift stays under a set limit.
- [x] R1.4 Build `providers/replay_carrier.py`: replays recorded timelines, including rep-leg teardown, unmatched legs,
      relay delay and track end before event.
- [x] R1.5 Add scripted variants: hang-up during dial, answer after hang-up, duplicate events, reversed order.
- [x] R1.6 Add the shuffled-order property test: every order reaches a terminal state, with one active call at most.
- [x] R1.7 Add lint bans as warnings (`scripts/rules.py`): bare `except Exception`, `.data.get(` in new core, `simulation` outside the
      provider, `tenant_id` in new core.
- [x] R1.8 Add the caps in section 12 to `scripts/rules.py`, as warnings for now. Run it and the R1.7 bans in CI.
      Baseline, November 7: backend 39,428 lines, frontend 22,223 with CSS, tests 29,407, 57 tables, 162 routes,
      largest function `save_disposition` 301, largest file `styles.css` 2,506; 20 silent `except Exception`, 119
      `simulation` references outside the replay carrier.
- [x] R1.9 Remove the retry from `finishCall` in `tests/simulation.ts`; find and fix any save conflict it hid.
- [x] R1.10 Save baselines for `scripts/bench.py`, `scripts/size.py` and the three `check.py` tiers.

Tests that are expected to fail until a later phase are marked as expected failures, so `main` stays green and CI flags one
that starts passing.

Done when: the known bugs not fixed by the hotfix fail these tests, and the suite runs in WebKit locally and in CI.

### Phase 2: delete the cut list

Goal: subtract before restructuring. The app still calls after every step.

Order, each as its own commit with its tests removed:

- [x] R2.1 Hosted mode, PostgreSQL, `deploy/`, connected sign-in, Windows build. In CI and `check.py`, remove the PostgreSQL matrix and
      services, the hosted browser specs and the `deploy/backup` lint paths. In `release.yml` and `release_manifest.py`, go Mac only, keeping the updater manifest
      valid for installed copies. Update `AGENTS.md`.
      Done November 7. Password sign-in, users and roles stay until R3.4, which removes them with sessions; the
      Windows `#[cfg(windows)]` blocks in `lib.rs` stay until it is reworked (R2.6, R4.10). The local/hosted browser
      spec loops keep their local variant until the specs are rewritten (Phase 10).
- [x] R2.2 Capacity planner, forecasts, staffing, work time, report projections, home metrics, billing reconciliation.
      Done November 7. The optimization report keeps its projections for StatML until R8.4; the per-number ceiling is
      the configured daily cap; staffing fields stay in the optimization config, unused, until R3.5 types it.
- [x] R2.3 Experiments UI, comparisons, trials, drift, treatment variants, conversation associations; keep holdout
      assignment and training. Done November 7: routes, screens and tests removed. Saved trial and comparison state is
      still read inside projections and is removed with them in R8.4 to R8.6.
- [x] R2.4 Coaching: practice, goals, library, snippets, trackers, comments, replay, strategies; rubric scorecards,
      appeals, review queue. Done November 7: routes, screens and tests removed. The live helper still reads reviewed
      examples from `coaching/library.py`, and post-call finalization still computes scorecards; both go with R10.1 and
      R10.2, which rebuild those paths.
- [x] R2.5 Compliance: registries, snapshots, consent evidence, RND import, scheduled screening; keep suppressions, hours
      and manual check. Done November 7. Opt-out effects on a live call moved into `compliance/suppressions.py`; the
      prepaid lookup stays as the manual check until RealValidito replaces it (R9B.17). The registry and consent tables
      stay until R2.9.
- [x] R2.6 SSH tunnel (Rust and UI), direct webhook route, ingress check, API-key and LiteLLM settings, script proposals
      and tests. Done November 7, except the direct `/webhooks/telnyx` route: 20 acceptance tests deliver signed carrier
      events through it, so it goes in R4.7 when the relay rework moves them onto the replay carrier. The LiteLLM chat
      loop in `providers/inference.py` stays for the R9.10 fallback.
- [x] R2.7 Dead voicemail drop: `VM_*`, `voicemail_asset`, `detached`, playback, gather, recording and late-human paths.
      Done November 7. A saved playback, gather or recording command from an older version is cancelled, never sent.
- [x] R2.8 Routes the UI never calls, after confirming each is dead. Done November 7: 117 routes, down from 162.
      Removed manual model training and activation, the optimization train trigger, the memory download and seller
      approval. Kept, though the UI does not call them: the job and control runners and `/v1/events` (test seams that
      share the live stream's code), research batches (re-preparing a lead; R9.15), agent recovery (R4.6), password
      sign-in (R3.4) and the direct webhook (R4.7).
- [x] R2.9 Unused tables, with a test that lists the allowed tables. Done November 7: 53 tables. `companies`,
      `consent_evidence`, `evaluation_reports` and `work_intervals` are retired: restore skips them and R3.7 drops them
      from the copied store. Coaching and scoring tables stay until R10.1 and R10.2 remove their last readers.
- [x] R2.10 Replace the 10-minute refresh churn with the once-a-minute rule from section 8. Done November 7: a refresh is
      queued only after a call is finalized, at most once a minute; an unchanged refresh writes nothing and publishes no
      screen event. Nightly training stays with R8.7.

Done when: L1 and L2 pass on replay, the kept acceptance tests pass, and the backend is about 20,000 to 24,000 lines
with restructuring still to come.

Phase 2 result, November 7: replay (L1, L2 and the recorded calls) and the kept acceptance tests pass. The size target is
missed: the backend went from 39,428 to 34,716 lines (12%), against 20,000 to 24,000. Routes fell from 162 to 117 and
tables from 57 to 53. Most of the remaining cut sits in modules later phases rewrite rather than delete: projections and
optimization workflows (Phase 8), coaching and post-call (R10.1, R10.2) and telephony (Phase 4). Phase 3's measurement
(R3.1 to R3.10) re-baselines the estimate.

### Phase 3: clear structure and typed data

Goal: remove the shared object, make core data typed, and move the owner's data safely.

Under D24, R3.1 builds `Ctx` and the empty package layout; R3.2, R3.4, R3.5 and R3.6 finish in the phases that rewrite
each area, and `Dialer`, `Application` and `Service` are gone by the end of Phase 10.

- [x] R3.1 Create `Ctx` and the package layout in section 4. Move code one package at a time, with tests passing.
      Done November 7: `platform/` holds db, config, jobs, secrets, security, the log file and `Ctx`; the other section 4
      packages exist and fill as their phases rewrite each area (D24). `Service` builds the `Ctx` and its carrier comes
      from `providers/carrier.py`.
- [x] R3.2 Replace mixins with module functions. `Dialer`, `Application` and `Service` go.
      Done November 10: the 19 mixin classes became 324 module functions that take the backend state first;
      `app/ops.py` finds each by name and loads its module on first use, so modules never import each other in a
      cycle. `AppState` keeps only state. A test checks every home module is named for the frozen sidecar.
- [x] R3.3 Add an import-direction test. Done November 7: `tests/test_imports.py` ranks the packages, bans any import
      from a new package into legacy code, and lists the 9 existing edges with the step that removes each; the list can
      only shrink.
- [x] R3.4 Remove `p`, tenant and seller scoping, roles, password login and sessions; keep the local token.
      Done November 10 for roles, password login, sign-out, teammates and the sessions table: the launch token buys a
      session held in memory. `p` stays as the workspace's three IDs because every stored row carries them; dropping the
      columns would rewrite the owner's data for no behavior change.
      Seats (D25) do not bring any of these back: a seat's identity is its HubSpot owner, its Telnyx credential and its
      relay workspace.
- [x] R3.5 Add Pydantic models for Call, Lead, Number, Task, Reservation, Command and Script with `extra="forbid"`.
      Done November 10, with one change: core packages read Call, Reservation, Lead attempts, Command, Number, leg and
      user records through typed views (`calling/records.py`), and only records a package wholly owns forbid unknown
      fields (the agent leg and presence). A `forbid` model over a record that legacy code still writes would turn a
      field nobody listed into a failed dial at run time. The `.data.get(` ban holds in every core package.
- [x] R3.6 Remove edit-version conflict checks now that there is one writer; keep the idempotent dial and hang-up keys.
      Done November 9: an outcome, a task, a meeting review, a number, calling hours, callback routing and settings
      save from any read; the last save is the record and a retried request lands once (its request ID). Kept where
      a second writer exists or money or the carrier is involved: a background script adoption, a paid lookup, carrier
      route setup and recovery actions.
- [x] R3.7 Write the migration: copy `local-2` to `local-3`, drop cut tables, transform documents, mark test calls.
      Done November 7: `platform/migrate.py`, run by the backend on first launch (`--migrate-from`, passed by the shell).
      The copy is transformed and counted before it becomes the store; SQLite `user_version` 3 marks it, so later
      phases can add in-place steps. New calls now carry `is_test` at dial and at wrap-up.
- [x] R3.8 Migration tests on a copy of the real database: counts match for 296 leads, 823 facts, 384 script versions,
      6 numbers, 7 calls and 13 tasks; master v3 and credentials still work; `local-2` is byte-identical.
      Done November 7 on a scratchpad copy (`OPENDIALER_REAL_STORE`): every count matched, 4 test calls marked, the
      source hashed the same before and after, the master script stayed version 3 (31,827 characters, 3 history
      entries), the saved Telnyx key decrypted and the workspace API served the copy.
- [x] R3.9 If migration fails, the app says so and stays on untouched data. Done November 7: the partial copy is
      removed, a note is left in `local-3`, the backend exits with code 78 and the shell shows the reason and that
      `local-2` is unchanged; the next start retries. An old version still running is a failure, not a copy.
- [x] R3.10 Turn the lint bans and size caps from warnings into CI failures for moved code. Done November 7: every ban
      and the per-file and per-function caps fail in the new packages; in `platform` the silent-except ban fails, and
      `tenant_id` and the simulation setting stay warnings until R3.4 and R4.7.
- [x] R3.11 Add Lead fields `source`, `external_id`, `signals` and `number_status`; migrate existing leads as `source: csv`.
      Done November 7: CSV imports write `source: csv` and `number_status: not_checked` (`leads/record.py`).

Done when: size is measured against the budget, and estimates are corrected in the Status section.

Phase 3 result, November 7: R3.1, R3.3, R3.7 to R3.11 done; R3.2, R3.4, R3.5 and R3.6 move with the phases that
rewrite each area (D24). Size: backend 34,932 lines, frontend 17,307, tests 23,620, 53 tables, 117 routes. The backend
estimate stays at about 17,100 at the end, but nearly all of the remaining cut now lands in Phases 4, 8, 9 and 10 as
each rewrite deletes the mixin code it replaces, not as a separate pass.

### Phase 4: call engine and guarantees

Goal: one state machine, one gate, and guarantees G1 to G8.

- [x] R4.1 `calling/state.py`: one pure transition table; generate the TypeScript state list.
      Done November 7: every state write goes through `transition()` (a test bans literal state writes elsewhere),
      every non-terminal state has a limit and an exit that reaches a terminal state, and
      `scripts/gen_call_states.py` writes `state/callStates.ts`, checked for drift. `telephony/reducer.py` is gone.
- [x] R4.2 `calling/events.py`: `apply_event` split into small steps, each testable alone.
      Done November 7: hang-up facts, carrier block, carrier outcome, machine, screening and answer are pure steps with
      their own tests; the legacy handler only stores their changes and starts the follow-on work.
- [x] R4.3 `calling/blockers.py`: one function; remove every other gate; each code has a message and fix.
      Done November 8: dial, the queue, preflight and the relay all read `blockers()`; `/v1/blockers` lists them for the
      screen. Every refusal is a 409 with `code`, `message` and `fix`. A test bans readiness codes outside the gate and
      the modules that supply its facts; the audio-leg setup check joins `calling/` in R4.4.
- [x] R4.4 `calling/audio_leg.py`: one fresh leg per call, client stays registered, credential verified once per session.
      Done November 8: the leg code left the mixins for `calling/audio_leg.py` with typed `AgentLeg` and `Presence`
      records; the Telnyx credential check runs once per app session instead of on every leg; the replay carrier answers
      legs itself (`instant_agent_legs`), so `calling/` never branches on simulation. The 1.5-second reconnect target
      is measured live (R4.15).
- [x] R4.5 `calling/dial.py`: keep durable dial and hang-up records and read-only reconcile; `save_disposition` under
      120 lines.
      Done November 10: dial, hang-up, their claims and receipts and the shared call finish live in
      `calling/dial.py` under the core rules (typed reads, `workspace()`, no simulation branch: the carrier says whether
      it replays). It reaches compliance, numbers and scripts through `app.ops`.
      Partly done November 8: `save_disposition` is 66 lines, and dial and hang-up keep their durable records (crash
      tests in R4.13). Moving the dial code into `calling/dial.py` is carried to Phase 9: it calls compliance
      authorization, script selection and number choice, which get module homes there.
- [x] R4.6 `calling/recovery.py`: one automatic pass at startup and every minute; open calls, uncertain dials, stuck
      hang-ups. Needs attention lists only what software cannot settle.
      Done November 8: every call records when it entered its state; `calling/recovery.py` decides when each
      unfinished call is due and what the pass does: read a quiet live leg, hang up a call that overran its state, end a
      dial that was never sent, mark an unanswered one unconfirmed, and end what the carrier can no longer settle with a
      review flag. Before this, a call stuck ringing waited two hours. Needs attention itself is R7.6.
- [x] R4.7 `calling/relay.py`: gate on HTTP exchange success; Realtime only wakes it; handle restart lease conflicts
      without a block.
      Done November 8, in `managed_webhooks.py` (it moves under `calling/` with the dial rewrite): a fresh HTTP exchange is
      the only gate, Realtime only wakes the loop, and the loop keeps polling every 20 seconds when Realtime drops. The
      relay identity now survives restarts; before, every restart drew "active on another computer" for up to 45
      seconds. A real conflict has its own blocker. The direct `/webhooks/telnyx` route is gone; its tests deliver
      through the relay path.
- [x] R4.8 Poll carrier status during an active call as the fallback for missed events. Done November 8: a live
      leg with no state change for 30 seconds is read from the carrier, every 30 seconds, and finished when it is gone.
- [x] R4.9 Save carrier outcomes directly on hangup. Done November 8: no answer, busy, voicemail and carrier
      failure are saved as soon as the hang-up event commits, before any job; the after-call job still runs for the
      summary.
- [x] R4.10 Sidecar auto-restart in `lib.rs`, then a recovery pass; UI reconnects by itself.
      Done November 8: an unexpected backend exit restarts it after a short backoff, at most 5 times a minute, never
      while quitting or after a busy or migration exit; the window follows it to the new address and the session
      carries over. Its first maintenance tick is the recovery pass. The Windows-only shell code is gone. The kill test
      on the packaged app is live check L13.
- [x] R4.11 Keep the Mac awake while Ready. Done November 8: the shell holds `caffeinate -d -i -w <app>` while the
      rep's presence is Ready and releases it on Pause; it ends with the app. Checked live in L12.
- [x] R4.12 Call timeline visible in Review; one log file with rotation.
      Done November 8: `calling/timeline.py` merges the call's own steps, carrier events (with who hung up) and sent
      commands; Review shows it for each call, checked in WebKit. The rotating log file came with R0.13.
- [x] R4.13 Crash tests: kill at each dial and hang-up step; no double dial, no lost call.
      Done November 8: `tests/test_crash_steps.py` stops the process at four dial steps and three hang-up steps; after
      recovery each lead has one call record, the carrier placed at most one call, and every call has ended.
- [x] R4.14 Fault injection for every row in the outage table in section 5.
      Done November 8 for the rows that exist now: Telnyx down at dial (never resent, settles with a review flag) and at
      hang-up (sent when it returns), relay down (named blocker, hang-up still works), missing speech (warning only),
      live helper down (calling continues) and a lost microphone (named blocker that clears). The ChatGPT, DDGS,
      HubSpot and Treg rows are tested with their code in R9.13, R9B.5 and R9B.14.
- [ ] R4.15 Live L1, L2, L3, L9, L10, L13.

Done when: all automated guarantees pass and the live checks pass. **Owner sitting 2.**

### Phase 5: UI kit and Dialer

Goal: nothing moves, nothing gets covered, and the rep always knows what is happening.

- [x] R5.1 Design tokens and eight components: Button, Field, Select, Check, Status, Panel, Toast, Table.
      Done November 8: `src/ui/kit.tsx` and `kit.css` (class selectors only, tested); the Dialer's bars use it.
- [x] R5.2 Replace global element CSS; add breakpoints; set a real minimum window behavior.
      Done November 10: every bare element rule sits in `@layer base`, so any component class beats it whatever its
      specificity; a test fails on an element rule outside that layer, inside media queries too. Screenshots of
      every page before and after match. Breakpoints at 1180, 1100, 900 and 860 pixels; the window cannot shrink below
      1024 by 720.
      In part, November 8: the stacked-checkbox defect is fixed and the Dialer has its breakpoint and pinned regions.
      The remaining global element rules go as Phase 10 rebuilds each page on the kit (R10.6 to R10.10).
- [x] R5.3 Three fixed regions: status bar, script with its own scroll, action bar. Reserved space for every phase.
      Done November 8: `calling/Dialer.tsx`; the action bar is pinned to the window and its space reserved.
- [x] R5.4 `screen.ts`: pure phase function with a `never` check; table tests for every phase. Done November 8.
- [x] R5.5 One state stream: `useCallState()`; remove `reservation`, `call`, `packet` and generation logic from local state.
      Done November 8 as `calling/useCallLoop.ts`: no reservation state and one read path for the call. One read
      counter stays: the browser suite proved that a slow read of the same version can otherwise bring back
      withdrawn advice, and a late read of the previous call can replace the new one.
- [x] R5.6 Background events do not refresh the call screen. Done November 8: the call view re-reads only its own
      call and published knowledge; other events only refresh blockers.
- [x] R5.7 Outcome keys `1` to `6`, with `wrong_party` among them; one key saves; Add detail holds note, callback time and
      more outcomes. Done November 8: 1 Meeting booked, 2 Callback requested, 3 Not interested, 4 Gatekeeper, 5 Wrong
      number, 6 Voicemail; Enter confirms a suggested outcome.
- [x] R5.8 Hang-up display with sound; countdown; clear finished lead after save. Done November 8; the next lead
      replaces the finished one when the countdown dials.
- [x] R5.9 Move Dial from and list choice out of the call view; remove Get next lead, View next lead, Start call,
      Pause after call and the voicemail checkbox. Done November 8: list choice and Check audio show only while paused.
- [x] R5.10 Error boundaries per region so the action bar survives a crash elsewhere. Done November 8.
- [x] R5.11 Render real script Markdown, including tables, without overflow; test with the owner's master script.
      Done November 8: tables render as tables that scroll in their own box. A copy of the owner's master script has no
      tables, so its rendering is unchanged; the owner's read-through is R5.13.
- [x] R5.12 Layout audit passes for every phase, using long names and the real script.
      Then set the browser suite's retries to 0 in CI, so any flake fails (one retry since R2.1).
      Done November 8 for every phase the simulation reaches, with a long lead name, in WebKit at both sizes; CI
      retries were set to 0. Reverted the same day: with no retry, CI showed older flakes the retry had
      hidden. Under load a test's click sometimes never reaches its handler (no request is sent) while the page
      re-reads the workspace many times a second. Open: find and fix that, then set CI retries to 0 again.
      Done November 9: the cause, the whole app redrawing on every background event, was fixed (`refresh.ts`
      keeps unchanged parts), and CI runs every browser test once (`retries: process.env.CI ? 0 : 1`).
- [ ] R5.13 Owner uses the Dialer on test calls and reports anything confusing.

Done when: the audit passes and the owner confirms it reads clearly.

### Phase 6: auto-dial and voicemail

Goal: the loop runs with almost no decisions.

- [x] R6.1 Failing tests for the behavior table in section 6. Done November 8: `tests/screen.test.ts` covers every
      phase and key; the browser suite drives the loop in WebKit.
- [x] R6.2 Auto-advance after Saved (3 seconds) and Skipped (1.5 seconds), with Space to go now and `P` to pause. Done
      November 8 with the Dialer loop.
- [x] R6.3 Voicemail: on detection, hang up at the later of detection and 7 seconds; final attempt holds; `S` stays;
      screening is not skipped.
      Done November 8: a machine result schedules the hang-up at the later of now and 7 seconds after the answer; a
      final attempt opens the line for a message; Stay and detected call screening withdraw a pending skip.
- [x] R6.4 Log detection against what really happened; report after 50 calls. Done November 8:
      `/v1/calling/voicemail-report`; the only ground truth is a call the rep kept open with Stay. The report itself
      waits for 50 real calls.
- [x] R6.5 Auto-dial stop conditions, each with a plain message. Done November 8: Pause, any blocker, an incoming
      call, an unsynced opt-out and an empty queue each stop the loop and say so; a failed dial pauses with its reason.
- [x] R6.6 Test-line allowlist enforced in code during test sessions. Done November 8: with `test_session` on, dial
      refuses any number but 203-821-1101, both when the call is prepared and when it is sent.
- [ ] R6.7 Live L3 to L8, L11 and L12.

Done when: a 20-call run passes. **Owner sitting 3.** Real lists wait for the lawyer check in R1.

### Phase 7: Settings

Goal: plainly what to add and where to get it.

- [x] R7.1 Failing tests: a row is green only after its real check; a failed check shows the reason. Done November 8
      (`tests/test_settings_rows.py`, and a WebKit test of saving and checking a key).
- [x] R7.2 Build the page in section 7 from `blockers()`.
      In part, November 8: the page opens with one status from `blockers()` and the eleven rows; the older cards stay
      below as the edit surfaces until they fold into the rows.
      Done November 9: rows only. The ChatGPT login, the call connection and calling hours are edited in their rows;
      the typed Telnyx connection fields and Check connection are gone (the setup file carries them); the optional
      national do-not-call lookup moved under Advanced.
- [x] R7.3 Check on save for each key; rows show what each powers and link to where to get it. Done November 8 for
      Telnyx and OpenRouter (one authenticated request each, stored with a fingerprint of the key); RealValidito,
      HubSpot and Treg get theirs in Phase 9B.
- [x] R7.4 Connection reconnects by itself; the setup-file import appears only when disconnected. Done November 8:
      the relay loop reconnects on its own (R4.7); the import field and Reconnect show only while it is down.
- [x] R7.5 Test call button to the saved test phone.
      Backend done November 8: `/v1/settings/test-call` adds the test phone to a "Test line" list and starts a test
      session; the queue offers only that list, dialing refuses any other number, and every call in it is a test call.
      The test phone joins 203-821-1101 on the allowlist because the row asks for your own number. The Test call and End
      test session buttons sit on the test phone row (WebKit test, November 8).
- [x] R7.6 Advanced section; Needs attention list. Done November 8: "More settings" is Advanced, and Needs attention is its first
      entry, listing only what recovery could not settle.
- [x] R7.7 Confirm the Telnyx and OpenRouter links land on the right screens. Done November 8 in the in-app
      browser: signed out, each opens its sign-in page with a redirect back to the API keys page
      (`redirectTo=/api-keys`, `redirect_url=…/keys`). Signed in, the owner lands on the keys page.
- [x] R7.8 Fresh-install walkthrough on an empty `local-3` copy.
      Done November 10 on the 2.0.0 backend with an empty folder (not simulated): welcome, name, then the Dialer
      names the first thing to add and Settings lists every row with where to get it. Found and fixed: five rows
      never set said "Failed" (now "Not set" with the fix), and the Dialer's blocker had no way to the page that fixes
      it (now an Open Settings, Numbers, Leads or Review button).

Done when: the owner can read the page without explanation.

### Phase 8: StatML

Goal: it can learn, and you can see that it is learning.

- [x] R8.1 Failing tests: test calls never enter datasets; goal `meeting_booked` labels at wrap-up. Done November 8:
      `meeting_booked` is the default goal, labeled when the rep saves it and retracted by a correction; a held meeting
      also counts as booked.
- [x] R8.2 Add `is_test`; clean existing rows in the migration. Done November 8: calls carry `is_test` (R3.7 marked the
      4 existing test calls), and training rows skip test calls and delivery tests.
- [x] R8.3 Add features from section 8, bumping the feature schema version; keep old rows usable. Done November 8:
      version 3 adds title seniority, industry, company size bucket, number source, trigger type, strength and age, and
      list, one-hot with an "unknown" column each; version 2 rows read as unknown. Role fit is left to the CRM (D15).
      Trigger fields fill once Phase 9 writes the lead record.
- [x] R8.4 Compute estimates directly from calls; remove the remaining projection machinery.
      Done November 8: the refresh counts selection estimates, retry continuation and number health in one pass
      over the campaign's calls (`optimization/projections.py`, 1,334 to about 190 lines), matched first against the old
      engine on the same calls. `reporting.py`, the report revision, its commit hooks and the `report_projections`
      and `report_revisions` tables are gone (retired in the migration); `/v1/optimization` is computed when read and
      the Call statistics panel left the Script page. Found and fixed: a held meeting recorded from the CRM did not
      count toward the booked-meeting goal.
- [x] R8.5 Automatic promotion from the existing out-of-time review. Done November 8: a model whose review passes
      replaces the live one at once; manual approval and the online trial are gone. Each promotion is audited.
- [x] R8.6 Automatic rollback using the 10% comparison group; record each promotion and rollback. Done November 8:
      each saved dataset compares goal rates since promotion; the comparison group beating the model by 5 points with
      100 decided calls in each group rolls it back, with the evidence audited.
- [x] R8.7 Training schedule from section 8; no UI churn. Done November 8: training starts at most once in 20 hours and only when
      labeled calls grew; the learning refresh announces a report only when it changed.
- [x] R8.8 "What it has learned" card in Review. Done November 8: calls used, best hours, best triggers,
      wrong-person rate by number source and by number check, the live model and its last change; groups under 5 calls
      are not ranked.
- [x] R8.9 Synthetic end-to-end test: a planted pattern in 600 calls gets learned, promoted, used and rolled back when it
      stops working. Done November 8 (`tests/test_learning_end_to_end.py`). The test found the review was a coin flip:
      on calls with no drift it passed 2 of 4 seeds, because the sigmoid sets the model's level from a 60-call window
      and a fixed 0.02 calibration tolerance treated that window's sampling error as a regression. The review now
      compares average miss against the baseline with a tolerance of the larger of 0.02 and two standard errors of
      that window (review v3), and the trees are smaller and slower. Across seeds the pattern is found in at least 7 of
      8 and noise is promoted in at most 1 of 8; measured over 20 noise seeds the false promotion rate is 1 in 20.
- [x] R8.10 Package the trainer sidecar on Mac and verify it runs, with that check in CI.
      Done November 8: `check.py sidecars` sends the R8.9 planted calls to the frozen trainer over the app's own stdin
      protocol, checks the reply and artifact as the app does, serves the model with the app's serving code (0.69
      against 0.24 for the planted trigger on this Mac) and starts the frozen backend. CI's Mac job runs it after
      building the sidecars; `check.py package` runs it after a local build.

Done when: the synthetic test passes. Real lift stays an open gate until enough real calls exist.

### Phase 9: research and fallback agent

Goal: criteria are enforced in code, and research keeps working if ChatGPT is unavailable.

- [x] R9.1 Seed `# Triggers` from the script's `# Before you dial`; the owner confirms windows and strengths.
      Done November 8 as an editor button that adds the default `# Triggers` list; the owner still confirms windows and strengths.
- [x] R9.2 Failing tests: out-of-window, unlisted and unquoted triggers are rejected; a `row_only` signal is capped at C; a
      `page` signal without a quote is rejected; one record through CSV and through HubSpot gives the same Lead; signals with
      mismatched counts are never paired by position; a 300-property record keeps its mapped signal columns; bookkeeping
      columns never reach a model; a `DEAD` number is not dialable.
      Done November 8 (`test_intake.py`, `test_trigger_criteria.py`, `acceptance/test_lead_intake.py`).
- [x] R9.3 One intake path and column map, with the owner's rows as the test case: delimiter per column, mapping before size
      limits.
      Done November 8: `leads/intake.py`; import shows the guessed map for correction; earlier leads get the mapped fields once at startup.
- [x] R9.4 `# Triggers` section in the parser and script editor.
      Done November 8: the parser reads `# Triggers` as rules, never as spoken text, with line-numbered errors.
- [x] R9.5 Fixed lead record with code checks.
      Done November 8: `prep/criteria.py`; `finish_research` is refused until the trigger passes.
- [x] R9.6 Agent instructions: row signals first; then search; decide; write.
      Done November 8.
- [x] R9.7 Review separates facts from the labeled bridge.
      Done November 8: a field marked inference is rejected only when contradicted, and then only the bridge is dropped.
- [x] R9.8 Show trigger, strength and evidence in the Dialer and Leads, including the "from the list, unverified" label.
      Done November 8: a Why now block in the Dialer and a line on Leads.
- [x] R9.9 Feed trigger fields to StatML and to first-call order.
      Done November 8: trigger fields were already StatML features; trigger strength breaks ties between equal first calls.
- [x] R9.10 `providers/chat_agent.py` from the `InferenceClient` chat loop, with `web_search` and `read_page`.
      Done November 8: `providers/chat_agent.py` and `providers/ddgs.py`.
- [x] R9.11 Add `ddgs` and confirm it works in the signed sidecar, with an import check in CI.
      Done November 8: `opendialer-backend self-check` loads it in the frozen sidecar, run by `check.py sidecars`.
- [x] R9.12 Fallback on `PlanLimit`, login failure or repeated timeout; store the route on each record.
      Done November 8: the same thread continues on the fallback, which has its own usage key and budget reservation.
- [x] R9.13 Force a ChatGPT failure and confirm identical output shape.
      Done November 8 (`test_prep_threads.py`).
- [ ] R9.14 Re-prepare 20 real leads on each route; the owner judges quality; measure cost, coverage and drop rate.
- [ ] R9.15 After the owner's OK, re-prepare active leads.
- [x] R9.16 Import reads a mapped live-status column; `DEAD` is not dialed and the lead says why.
      Done November 8.
- [x] R9.17 Keep number source per lead; feed it to StatML and the learning card.
      Done November 8: mapped profile fields and number source feed the features.
- [x] R9.18 Run T4 and T5 and record the results in section 9A. Done November 8 (section 9A).
- [ ] R9.19 Run T1 after the owner OKs using the saved Telnyx key; run T2 only if T1 is useful and the owner approves.
- [x] R9.20 Record the Treg testing result in section 9A and settle D18.
- [x] R9.21 Failing test: a CSV import makes no Treg, Trestle or RealValidito request, and its leads show "Not checked (CSV)".
      Done November 8.
- [x] R9.22 Number status and its reason show in Leads and the Dialer; auto-dial skips Conflict, Bad and No valid number; the rep
      can dial a Conflict by hand. The number-check work itself is in Phase 9B, because it runs only on CRM leads.
      Done November 8: holds are named on Leads and in the Dialer; dialing a lead by hand overrides only Conflict.
- [x] R9.25 The learning card shows wrong-party rate by number status as well as number source (T3). Done November 8;
      `number_status` is also a learning feature.

Done when: no lead claims a trigger that fails the criteria, and the owner accepts the 20-lead sample.

### Phase 9B: HubSpot

Goal: HubSpot picks who is called and every call lands on the contact. Calling never depends on it.

Tests first:

- [x] R9B.1 Capture recorded HubSpot responses from the owner's account, with the test contact only: list members, contacts,
      companies, dispositions, call create, errors and rate limits. These are test fixtures; the product has no simulated
      HubSpot.
      Done November 10 for the reads: `tests/fixtures/hubspot/recorded-2026-11-10.json` (key check, lists, the test contact, the 7 dispositions, a refused key) drives `tests/test_hubspot_recorded.py`. The account has no lists yet. Writes were recorded live on November 9 (`docs/evidence/rebuild/2026-11-09-hubspot-treg-live.md`).
- [x] R9B.2 Failing sync tests: paging; one contact appears once; syncing again changes nothing; HubSpot edits update the row
      without resetting attempts; a removed contact stops being dialed and keeps its history; no number is not dialable; an
      invalid phone is reported.
      Done November 8 over answers shaped from HubSpot's docs (`test_hubspot.py`, `acceptance/test_crm_flow.py`); R9B.1 replaces them with recordings.
- [x] R9B.3 Failing opt-out tests: a sync can add a suppression and never remove one; a local opt-out writes back.
      Done November 8.
- [x] R9B.4 Failing logging tests: every outcome maps, or setup says which does not; a retry after an uncertain write leaves
      one record; rate limits back off; a CSV lead is created only on its first connected call and never for a no-answer; the
      summary is sent and the transcript never is; test-session calls are not written unless the contact is on the test list.
      Done November 8.
- [x] R9B.5 Fault injection: HubSpot down during a sync, a call and a write; calling continues; the 24-hour blocker fires and
      names its fix.

Build:
      Done November 8 for a sync; a failed call write retries as a job.
- [x] R9B.6 `providers/hubspot.py`: Service Key auth, one pinned dated API version, paging and backoff.
      Done November 8 on v3 paths; the dated version is pinned when R9B.7 reads the account.
- [x] R9B.7 Settings row with a check on save; confirm and record the exact scopes and which property holds do-not-call.
      Done November 10: the check is a list read the sync needs (it used the owners endpoint, which one user no longer needs). Scopes: `crm.lists.read`, `crm.objects.contacts.read`, `crm.objects.contacts.write`, `crm.schemas.contacts.write`, all exercised against the owner's key on November 9 and 10. Do-not-call lives in the app's own contact properties `opendialer_opt_out` and `opendialer_opt_out_reason`, created at the first check.
- [x] R9B.8 `leads/hubspot_sync.py`: list picker on Leads, Sync button, sync at Start calling.
      Done November 8: the HubSpot panel on Leads; Start calling syncs, and every 30 minutes while calling.
- [x] R9B.9 `aftercall/crm_log.py`: `log_call(CallRecord)`, outcome map and a retrying job that ends in Needs attention.
      Done November 8: `crm_log.py` and the `crm` job lane.
- [x] R9B.10 Create the `phone_status` and `opendialer_call_id` properties once, at setup.
      Done November 8: created when the HubSpot row's check passes.
- [x] R9B.11 Last-contacted guard.
      Done November 8.
- [x] R9B.12 Layout audit covers the list picker, the spend limit and the Settings rows.
      Done November 8 (`crm-settings.spec.ts`).
Waterfall and do-not-call on CRM leads (D19, D23), tests first:

- [x] R9B.14 Port the 20 offline tests from `run_waterfall.py` as failing tests over recorded Treg responses, plus: a CRM
      number goes straight to Trestle; a repeat sync pays nothing; a changed CRM number is rechecked; the list limit stops the
      job and says so; no token or a Treg outage never blocks a lead that has a CRM number; a CRM number is never overwritten.
      Done November 8 (`test_waterfall.py`, 24 tests).
      Updated November 13: a usable saved phone or mobile number skips Treg and goes to RealValidito screening.
- [x] R9B.15 Failing do-not-call tests over recorded RealValidito responses: each of `federal_dnc`, `state_dnc` and
      `tcpa_litigator` suppresses with its reason; `invalid` marks Bad; batches of at most 1,000; results expire after 30
      days; no key, error 603 or 604, or an outage leaves "DNC not checked" and blocks auto-dial with a named fix; a sync never
      clears a do-not-call suppression.
      Done November 8 (`test_realvalidito.py`), over answers shaped from the public docs.
- [x] R9B.16 `providers/treg.py` (Trestle, LeadOcean, AI Ark, Tomba, Wiza with polling) with an idempotency key, and
      `leads/waterfall.py` as a job after sync with a durable ledger.
      Done November 8: the ledger lives on the lead.
- [x] R9B.17 `providers/realvalidito.py` and `leads/dnc.py`; replace the DoNotCallDNC provider and its tests.
      Done November 8; the old DoNotCallDNC path stays only as the manual check until R10.
- [x] R9B.18 Settings rows for the Treg token and RealValidito, each checked on save; spend limit and estimate on the list.
      Done November 8.
- [x] R9B.19 Write `phone_status`, an empty phone field and do-not-call opt-outs back to HubSpot.
      Done November 8.
- [ ] R9B.20 After your yes and limit: run the waterfall and do-not-call check on the HubSpot test list, then on 20 real
      contacts; record cost, confirmed share and do-not-call hits in section 9A.
- [ ] R9B.13 Live L14 to L19, using only the owner's test contact and 203-821-1101. L20 only if Nooks is in use.

Done when: L14 to L19 pass, a forced HubSpot outage does not stop a test call, the waterfall and do-not-call tests pass, and
R9B.20 is recorded. **Owner sitting 4.**

### Phase 9C: seats and the shared number pool

Goal: three reps call at once from one pool, never reach the same contact, and callbacks find the rep who called
(section 9C, D25, D26). Built on recorded responses; the live checks wait for the keys, the pool numbers and a second
phone.

- [x] R9C.1 Read the account's calls-at-once limit from the portal's "See privileges" page and record it in section 9C.
      Withdrawn November 10 by D27 (one user).
- [x] R9C.2 Failing tests over recorded HubSpot responses: a seat syncs only its owner's contacts; two seats never sync
      the same contact; reassigning the owner moves the contact on the next sync and keeps its history; an opt-out
      written by one seat suppresses the contact on the other after its sync; a call record carries the seat's owner.
      Done November 8 (`test_hubspot.py`, `acceptance/test_crm_flow.py`): owner filtering, reassignment, opt-outs through HubSpot and the owner on each call record.
- [x] R9C.3 Failing relay tests: two seats asking at once never lease the same number for the same minute; the daily cap
      counts every seat's calls; the area-code preference holds; a callback rings the last caller, and goes to voicemail
      and a task when that seat is away; an unknown caller goes to the owner's seat.
      Done November 8 (`test_relay_pool.py`, on a local PostgreSQL 16 with the real migrations and stubbed Supabase pieces): 10 simultaneous leases get 10 numbers; the cap counts both seats; area code then least used; a callback follows the last caller, else the owner. The harness caught two defects first: an ambiguous column that would have failed every lease on Supabase, and a lease race between snapshot and lock.
- [x] R9C.4 Failing tests: a Telnyx channel-limit refusal pauses only that seat with "Telnyx account call limit reached"
      and its fix; a relay that cannot lease a number blocks the dial with a named blocker.
      Done November 8 (`calling/carrier_errors.py`): recognised by its words until L22 records the exact code.
- [x] R9C.5 "Calls for" row on Settings, set at seat setup, read-only after; sync filters by it; `log_call` writes it.
      Done November 8: chosen from HubSpot's users, refused once set (`CALLS_FOR_SET`).
- [x] R9C.6 Sync at Start calling and every 30 minutes while calling; a CSV file imports on one seat only.
      Done November 8. A CSV file lives in the one seat's own store that imported it, so no other seat can call it.
- [x] R9C.7 Relay: migration for pool numbers, leases, daily counts and last caller per prospect, with isolation tests
      in `supabase/tests`; the shared incoming route; the webhook routes a callback by last caller.
      Done November 8: `20261109010000_shared_number_pool.sql`. Not applied to the live relay: that needs the owner's OK, because it replaces `relay_receive`.
- [x] R9C.8 `numbers/pool.py`: lease before each dial and release at the end; the dial presents the leased number;
      number health is read from the relay's counts.
      Done November 8 (`numbers/pool.py`, `test_number_pool.py`): no lease, no dial, a named blocker, nothing counted; the maintenance pass releases ended calls.
- [x] R9C.9 Seat setup: from a rep's API key, create the Call Control application with a channel cap of 2, the telephony
      credential and the relay workspace, and write the private setup file; it never changes another seat.
      Done November 8 (`numbers/seat.py`, `relay_invite_seat`, Team calling in Settings): only the owner's seat can invite, on its own account; the seat file carries the shared keys and turns the pool on.
- [x] R9C.10 Research route per seat: the ChatGPT login or the OpenRouter fallback as main route, chosen in Settings.
      Done November 8.
- [x] R9C.11 Layout audit covers the new rows and blockers.
      Done November 8: the Settings page stays under 30 visible controls (optional keys open with Add; Team calling is folded).
- [x] R9C.12 Withdrawn by D27 (one user). Live L21 to L24 with the owner's test contact, 203-821-1101 and a second approved phone.

Done when: the tests pass, L21 to L24 pass, and a forced relay outage stops dialing on every seat with a named blocker
and never lets two seats call one contact. **Owner sitting 5.**

### Phase 9D: Windows

Goal: the same app on Windows, signed and self-updating, so a seat can be a Windows PC (D2). Built after Phase 9C so the
calling core, seats and pool are settled first; the Windows work is packaging and the few system-specific pieces.

- [x] R9D.1 CI: a `windows-latest` job runs the backend tests, freezes the sidecar, runs `scripts/trainer_check.py`
      against it and builds the desktop app; `check.py full` stays the Mac gate and CI is the Windows gate.
      Done November 8: green on `windows-2022` (backend tests, frozen sidecars, shell tests). Windows-only defects it
      found are fixed: `cmd.exe` quoting in `check.py`, and the onefile sidecar's child holding files after a stop.
- [x] R9D.2 System-specific pieces, each behind one small function with a test: keep-awake (`caffeinate` on Mac,
      `SetThreadExecutionState` on Windows, G8), the app data and private key file paths and their permissions,
      microphone permission prompts, and sidecar start and stop.
      Done November 8 for keep-awake (`SetThreadExecutionState` on one held thread) and the updater's wait for the backend files; app data and private files sit in the user's own profile, protected by its ACL rather than mode bits. Microphone permission is checked live in L25. Found and fixed: the training child dropped `SYSTEMROOT` (Windows names it in upper case), so StatML could never train on Windows.
- [x] R9D.3 Browser suite and layout audit also in Chromium, which is what Windows' WebView2 uses; the Mac stays WebKit.
      Done November 8: 139 of 139 pass in Chromium. It found a real Dialer loop: pausing released the audio, whose "disconnected" report paused again, hundreds of times a second, and the rep never saw why calling stopped. Fixed with tests.
- [ ] R9D.4 Signing: Windows code signing (Azure Trusted Signing or an OV certificate; owner input) in `release.yml`.
      In part, November 8: the release builds signed-for-update NSIS and MSI installers and signs them with
      Authenticode through `WINDOWS_SIGN_COMMAND` once the owner sets up a route; until then the build warns.
- [x] R9D.5 Signed updater for Windows; `release_manifest.py` requires both systems' artifacts again. Carry the known
      update-harness lessons: Restart Manager stopping the sidecar, read-only files during replace.
      Done November 8 (`release_manifest.py`, `release.yml` matrix; the installer hook stops the old backend before replacing it).
- [ ] R9D.6 Live L25 to L27 on a Windows PC: install, test call with audio both ways, a Windows seat beside a Mac seat
      from the shared pool, then update and rollback with workspace data kept.

Done when: CI is green on both systems, L25 to L27 pass, and a signed Windows build updates itself. **Owner sitting 6.**

### Phase 9E: calling never stops by surprise

Goal: any problem, in any area, that would stop a call is found before the rep meets it, and repaired by itself where
software can (owner direction, November 9). Two defects reached the owner on November 9: a relay delivery gap that only
a hidden button could clear, and a Pause click lost while the button was swapped by a stale re-read. Both passed every
existing test, because each test checked one piece and none asked whether every stopped state has a way out.

- [x] R9E.1 One calling model: a small pure state machine of everything that decides whether the next dial happens:
      setup and keys, relay connection and delivery, numbers and the pool, leads and their holds, the queue, rep
      audio, presence, Pause and Start, the call itself, after-call, opt-outs and recovery. `blockers()`, the Dialer
      loop and the relay loop read it; nothing else decides.
      Done November 10 as two pure halves that meet at the API: `calling/decide.py` answers "does the next dial
      happen?" from the blockers, the rep's own claim, the reservation and the queue, and the queue and the dial refuse
      only with its answer; the Dialer's side (Pause, Start, keys) is `calling/screen.ts`. The relay loop's steps are
      `calling/relay.py`, whose failures arrive as blockers. `tests/test_calling_model.py` checks the laws over every
      blocker alone and in pairs and every claim, step, reservation and queue answer.
- [x] R9E.2 Exhaustive check of the model in CI: every reachable state to a set depth, under every event order. Laws:
      every stopped state has an exit (it clears itself within its stated time, or shows a visible control that clears
      it); Pause is always honored; no lead is dialed twice at once; an opt-out always holds; no state claims to clear
      itself unless a path does.
      Done November 9: `tests/screen-model.test.ts` checks every Dialer input combination (over 10,000) and `test_blockers.py` every combination of blocker facts.
- [x] R9E.3 Every blocker code has a test that drives it in and back out through its real code path, not a table.
      A new blocker without one fails CI.
      Done November 9: `tests/acceptance/test_blocker_exits.py` drives all 21 blockers in and out; `EXITS` in `calling/blockers.py` declares each way out.
- [x] R9E.4 Fault soak: thousands of seeded runs in CI against the real backend in simulation, injecting the app
      closed for days, relay gaps, dropped, duplicated and late events, provider outages from section 5, slow
      re-reads, a killed backend, sleep and wake, a removed mic and clicks during redraws. The R9E.2 laws are checked
      after every step; a failing seed is kept as a regression test.
      Done November 9: `tests/test_fault_soak.py`, 25 seeds in CI; 2,000 and then 1,000 seeds (15 leads, 50 steps each) passed locally.
- [x] R9E.5 Readiness self-test while idle: every minute while the app is open and not calling, run the same checks a
      dial would (keys valid, relay fresh, a number ready, audio device present, queue has a callable lead, the next
      lead's script prepared), so a problem shows before Start, not after it.
      Done November 9: `/v1/calling/readiness`; the paused Dialer shows how many leads can be called now, or why none, every minute.
- [x] R9E.6 Watchdog: a self-clearing blocker that outlives its promise triggers its repair automatically and is logged;
      if the repair fails, the screen shows the real fix control instead of "clears by itself".
      Done November 9: `calling/watchdog.py`, run by the maintenance tick; a stuck connection shows Reconnect in the Dialer and in Review.
- [x] R9E.7 Rebuild the relay connection (`managed_webhooks.py`) as a plain module on the model, as Phase 4 did for
      the dial loop.
      Done November 9 as decisions: `calling/relay.py` `after_exchange` and `after_failure` decide every step of the loop and are checked over every combination; `managed_webhooks.py` only carries them out.
- [x] R9E.8 The Dialer shows the rep's own Start and Pause at once and never derives a control from a re-read that
      may predate it (done November 9 for Start and Pause; extend to every control).
      Done November 9 for every control: Hang up, Skip, Stay and the outcome keys are one intent overlay
      (`withIntent` in `calling/screen.ts`), checked over every input; one save at a time. Found and fixed: an outcome
      key pressed while the call was still ending was silently dropped; it is now kept and sent once the end is
      confirmed. Browser tests hold the server's answer and serve stale re-reads.
- [ ] R9E.9 Optional, owner decision: a scheduled real test call to the owner's test line to prove the carrier path
      end to end. It is billable, so it runs only with the owner's yes and a limit.

Done when: R9E.2 and R9E.4 run in CI with no failures, every blocker has its in-and-out test, and a week of real
use logs no stop that the watchdog or self-test did not catch first.

Bend 2 (HigherOrderCO, September 2026) checks written laws with proofs at compile time. It fits R9E.2 in spirit, but it
is new, has no editor support and would duplicate the model in a third language, so R9E.2 is written in Python. Revisit
once the model is small and stable.

### Phase 10: helper, summary, remaining pages and release

Goal: finish the remaining surface and ship.

Live helper and after-call:

- [x] R10.1 Live helper: scripted tips and private help only; remove interpretation layers that nothing else uses.
      November 11: automatic tips and private requests pick one spoken response from the call's prepared script.
      The app validates the completed selection and displays the prepared wording verbatim. Phone screening shows
      the prepared Screening line directly.
      November 10, after the owner's live calls: each tip now also pulls up the one script part that fits the moment
      (the model names it from the numbered script), shown under the tip; the word-matching "current stage" is gone
      from the live path. The live transcript that feeds it failed on every auto-dialed call (a WebKit audio engine
      started without a click) and is fixed.
      Done November 9: `helper/live.py` and `live_help.py` replace `coaching/` and `postcall/`. One streamed request
      per turn; the tip lives in memory and is never saved. Gone with it: the finalize and review jobs, scorecards,
      rubrics, trackers, commitments, coach feedback, coaching strategies and the playbook (the script's Facts and
      Never say are the one place). The next call's script and brief read the summary instead. 10 tables retired;
      backend 38,623 to 32,403 lines.
- [x] R10.2 After-call: one request over the whole transcript returns summary, suggested outcome and next step.
      Done November 9: `aftercall/summary.py`, one request over the whole transcript on OpenRouter's no-retention route; saved in `call_summaries`; the wrap-up shows it and HubSpot gets it. The old finalize path still runs beside it until R10.1 retires it.
- [x] R10.3 Fix the three verified defects by design: no batch-wide rejection, no 32-segment cutoff, no "partial" caused by an
      unassessed item.
      Done November 9 by design in `aftercall/summary.py`: no page limit, each field validated alone, no partial state.
- [x] R10.4 V4.1 Flash reviews commitments that would create tasks; a failed item fails alone, with Retry.
      Done November 9 by design: the summary is the one AI step after a call and returns its next step; it never
      saves an outcome or creates a task (the rep's outcome does, section 6). A failed summary fails alone and has
      Retry in the wrap-up and in Review.
- [x] R10.5 Transcript retention of 30 days; remove the raw-speech store and expiry code; add a delete control.
      Done November 9: text is saved on `transcript_segments` for 30 days, purged by the maintenance tick, deletable from Review; an opt-out deletes it. The in-memory store stays only for the live helper.

Pages:

- [x] R10.6 Leads and Lists become one page.
      Done November 9: the lists table sits above the leads on Leads; the Lists page is gone from the menu.
- [x] R10.7 Numbers: list, buy, add, ready status, daily cap.
      Done November 9: each number is one row (ready status, calls today against its cap, Set up route, Pause or
      Resume). The manual evidence-and-workload review, imported spam labels in the UI and lead reassignment are gone;
      Resume keeps the same checks (route, test call, health) in one click.
- [x] R10.8 Script: clear "unsaved draft" state versus saved version; memory view.
      Done November 9: a Saved or Unsaved state with Discard; usage and rewrite tools folded.
- [x] R10.9 Review: calls, timeline, follow-ups as compact rows, learning card.
      Done November 9: Review opens with Needs you, then follow-ups, calls and the learning card; a call shows its
      outcome, summary, timeline, transcript and the rep's note. Each follow-up is one row with Complete and Cancel;
      its time and reminder fields fold under Change unless it needs a decision.
- [x] R10.10 Layout audit passes on every page.
      Done November 9: `tests/layout-audit.spec.ts` covers every page at both sizes; it caught the Leads toolbar shifting 43px, fixed by moving the lists table below the leads.

Docs and release:

- [x] R10.11 Rewrite README, GETTING_STARTED and ARCHITECTURE; move finished plans to `docs/history/`; simplify `AGENTS.md`.
      Done November 9: README, GETTING_STARTED and ARCHITECTURE rewritten; 16 finished plans moved to `docs/history/`; AGENTS.md simplified.
- [ ] R10.12 Full live matrix L1 to L19 and L21 to L24 on Mac, L25 to L27 on Windows, plus L20 if Nooks is in use.
- [ ] R10.13 Signed 2.0 through the gated release, on the owner's OK; verify installed data, version, signatures and rollback.
- [ ] R10.14 Confirm CI matches the "after the rebuild" table in section 12A and tag `v2.0.0`.

Done when: every live check passes on Mac and Windows and the owner signs off. **Owner sitting 7.**

### Phase 11: finish 2.0 (owner approved November 10)

Found live and decided by the owner on November 10: callbacks never reached the app (every one so far heard the
greeting, because the app took a call only mid-session with audio up, and silently); a number bought November 6 still
reads "purchased" although Telnyx made it active that minute; forwarding waits; voicemail needs no new feature and
the skip stays (the rep presses Stay, or the last attempt holds the line); StatML is archived; stage drop-off is the
one learning view kept. Each item gets its tests first.

- [x] R11.1 Plan text: D29 to D32 and this phase.
- [x] R11.2 Numbers: any number the app holds as pending is re-checked against Telnyx at start and every few
      minutes, so a missed order event never leaves it "purchased".
- [x] R11.3 Callbacks ring in the app whenever it is open (D29): the call client stays registered while the app is
      open; the backend rings the app when its heartbeat is fresh and it is not on another call, for any caller; a
      ring panel on every page with the caller (lead name when known), Answer and Decline, a ring tone and a macOS
      notification when the window is hidden (built as a Dock bounce); answer gives two-way audio (transcript and
      helper on callbacks are not built);
      Decline or 20 seconds unanswered plays the greeting and keeps a missed-call task; outbound dialing waits while a
      callback rings or is live.
- [x] R11.4 Forwarding waits (D30): the forwarding field and offline reference are hidden; the greeting stays.
- [x] R11.5 Archive StatML (D31): tag `statml-archive`, then remove the learned model, training, comparison group,
      trainer program and its CI and release steps, and Review's learning card. The queue orders callbacks due, then
      never called, then longest since the last try, inside calling hours and company spacing. Script storage and
      per-number caps stay.
- [x] R11.6 Stage drop-off (D31): the after-call summary names the script part where the call ended, checked against
      the script's parts; Review shows where answered calls end, by outcome.
- [x] R11.7 HubSpot test segment (section 9B's rule, never built): test calls to a contact on the chosen test segment
      are written to HubSpot, so L15 to L18 run on the owner's line. Built simpler: a test session dials only the
      rep's phone, so a test call to any HubSpot contact is written, and the session prefers that contact.
- [x] R11.8 Call screeners: when the transcript shows a phone's screener, the helper shows "say your name and why
      you're calling" without a model call.
- [ ] R11.9 One candidate, installed; live sessions: calls (transcript, voice both ways, helper, screener, L3 to L7),
      callbacks (app open on any page, hidden, busy, missed, closed), failures (L9 to L13), HubSpot (L15 to L19, the
      first paid number search on the owner's yes, the do-not-call check once its key is saved); then R10.13 and
      R10.14.

### Phase 12: shared calling in 2.1

- [x] R12.1 One assigned HubSpot segment per computer profile, using the existing setup and sync.
- [x] R12.2 Relay-owned sticky number bindings, per-prospect dial admission, and a 90-attempt rolling outbound ceiling.
      A rep may use a pooled number without reassigning its incoming carrier route. Existing local bindings take
      precedence on their first pool dial if the number belongs to the pool and the relay has no binding yet.
- [x] R12.3 Verified carrier events attach return calls to the originating computer. A missed call and its prospect
      stay associated with that computer. Outbound capacity and unused outbound reservations cannot block ringing.
      The binding retains callback ownership after outbound attempt records expire.
- [x] R12.4 Test with disposable relay and desktop data, including overlapping calls from separate profiles, callback
      affinity at the outbound limit, and the in-app prospect view. These tests used the first implementation with
      separate Call Control applications and replayed carrier events. See the [local verification receipt](evidence/rebuild/2026-11-11-2.1-local.md).

One Telnyx account owns the numbers. Each environment uses one Call Control application, one credential-based SIP
connection and one relay endpoint at `/functions/v1/telnyx-webhook`. Each rep imports a private setup file under
**Settings → Call connection**. The file connects their computer to its own relay workspace, WebRTC credential and
assigned HubSpot segment. Telnyx API keys authenticate account requests. A key per rep is optional for independent
revocation. Telnyx supports multiple audio credentials on one SIP connection.
[Telnyx contact-center guide](https://developers.telnyx.com/docs/voice/webrtc/use-cases/contact-center) ·
[Telnyx telephony credentials](https://developers.telnyx.com/docs/voice/webrtc/auth/telephony-credentials)

- [x] **R12.5 Align the repository migration history with hosted Supabase.**
      On November 11 the connected [OpenDialer project](https://supabase.com/dashboard/project/wfucwxgeexzwhinrylyn)
      reported only two applied migrations. Its database has neither the number-pool tables nor the pool RPCs.
      The hosted receiver verifies Telnyx signatures and omits the `inbound_call` transfer field. Only the production
      OpenDialer project was visible in the connected account. The two October SQL bodies matched the local files
      by byte length and digest. Give the files the hosted IDs in the following table and keep their contents.
      The four November migrations were applied in order through the Supabase connector on November 11. Their
      repository filenames use the hosted migration IDs, and the SQL digests match.
      [Supabase migration deployment](https://supabase.com/docs/guides/deployment/database-migrations)

| October migration | Repository ID today | Hosted ID to adopt |
|---|---|---|
| `managed_control_relay` | `20261029091412` | `20261029092005` |
| `managed_connection_setup` | `20261029202925` | `20261029203132` |

- [x] **R12.6 Fix first-call audio for a new rep.**
      Reproduce `CALLER_NUMBER_ROUTE_REQUIRED` with a fresh profile and only a number supplied by the relay pool.
      Exercise `POST /v1/telephony/agent` with the production Telnyx client and a mocked HTTP transport. The first
      agent dial must use a pool-authorized caller ID and the rep’s own SIP address. Read the relay pool’s route
      readiness when starting audio. The internal audio leg uses no prospect dial capacity. Include cases for an
      unavailable pool and a standalone workspace.
      The earlier replay tests skipped the real audio setup branch. [Audio leg setup](../services/backend/app/calling/audio_leg.py)

- [x] **R12.7 Route the shared application’s signed events by call ownership.**
      Add an opt-in shared application to the pool in a new migration. The application’s signing route belongs to
      the pool and can verify events when a rep is disabled. Extend `relay_pool_submit` to record the authenticated
      workspace and expected endpoints before a prospect dial. Register the rep’s audio leg before its dial using
      the same account, application and workspace checks. The authenticated workspace and current installation lease
      determine ownership. On a matching signed event, bind Telnyx’s call-control ID to the workspace so subsequent
      events reach it even when they carry fewer fields. An unresolved outbound event stays out of rep inboxes until
      the relay can match it or reconcile it. Keep the current legacy application route for standalone profiles.

      Use the dialed number and the caller’s phone to find the last confirmed rep for a callback. An unsuccessful
      retry preserves the earlier owner. Match the original incoming call and its transfer leg to that same rep.
      Store the receipt and ownership together and deduplicate by Telnyx event ID. Retain the current private relay
      tables and grant each client RPC only the access needed by its scoped workspace.
      [Current reservation and callback SQL](../supabase/migrations/20261111192755_shared_number_pool.sql) ·
      [Telnyx webhook delivery](https://developers.telnyx.com/docs/voice/programmable-voice/receiving-webhooks) ·
      [Telnyx transfer context](https://developers.telnyx.com/api-reference/call-commands/transfer-call)

- [x] **R12.8 Generate the rep profiles and deploy the webhook receiver.**
      An operator selects the shared application, approved numbers and outbound voice profile once. Configure the
      shared SIP connection for internal URI calling with its outbound voice profile unset. Each rep gets a Telnyx
      Telephony Credential under that connection, a relay workspace and a HubSpot segment. Use the existing managed
      setup flow to issue the private profile. Configure the profile’s public URL
      and publishable key for the existing relay. Point the pilot application at the existing webhook endpoint.
      Deploy the reviewed receiver at that path. Preserve Ed25519 and timestamp checks,
      the `inbound_call` field and acknowledgement after durable storage. Supabase gateway JWT verification stays
      disabled for `telnyx-webhook` and `managed-setup`, which check the Telnyx signature and the operator-issued
      invitation respectively. The Supabase service key stays on the server.
      [Relay runbook](../supabase/README.md) · [Supabase function configuration](https://supabase.com/docs/guides/functions/function-configuration)
      The receiver and setup function were deployed on the existing project. Both live pilot workspaces are
      connected with separate audio identities and assigned test segments.

- [x] **R12.9 Play the greeting when the rep is offline (D29).**
      The relay queues incoming events for delivery after a reconnect. The desktop currently issues the greeting command.
      Add a durable claim for the original incoming call so the reachable desktop or a hosted fallback can handle it.
      Publish each rep’s WebRTC reachability to their scoped workspace and give an open desktop time to claim the
      call through its normal relay exchange. If the rep is offline or the claim window expires, the worker answers, plays the configured greeting
      and leaves one task for the assigned rep. Reconcile existing Telnyx commands before retrying a timed-out claim.
      Skip calls the caller has already ended. Store the fallback worker’s authorized Telnyx account key as an Edge
      secret. Use the recorded Telnyx webhook and durable retry state to recover from worker interruptions.
      [Current offline capability](../services/backend/app/administration.py) ·
      [Supabase function scheduling](https://supabase.com/docs/guides/functions/schedule-functions)
      Code and replay verified. The worker is deployed. Its Edge secrets, Vault token and schedule are needed for
      the live greeting check.

- [x] **R12.10 Prove the software locally before live calls.**
      Run the tests for a fresh audio leg, the shared application’s routing, two profiles syncing distinct HubSpot
      fixtures, a rep disabled on the shared route, repeated and out-of-order events, and one callback task per
      call. Run the migrations on a disposable local Supabase stack. Verify attempts 90 and 91, a callback at the
      cap, and a retry that preserves the prospect’s caller ID. Launch both packaged profiles simultaneously on this
      Mac and exercise the callback panels and first-call setup. Run the fast and full tiers and require Verify to
      pass on the candidate commit. Record replay and package evidence before the hosted pilot.

- [ ] **R12.11 Run the two-rep pilot on the existing relay.**
      On November 11 the owner approved deploying to the existing OpenDialer project before the live pilot.
      The migrations and functions are deployed. The Mac renewed its lease with all four registered numbers,
      and temporary profiles passed signed routing checks on the hosted relay. The owner approved using the current
      Call Control application with its existing Telnyx number assignments. Use the current Mac profile and a
      second app instance with its own workspace and audio credential on the same Mac. Set up the callback worker
      before the closed-app check. Record the results and timings under `docs/evidence/rebuild/`.
      [Supabase hosted targeting and dry runs](https://supabase.com/docs/guides/local-development/local-and-hosted-projects)

```sh
supabase db push --remote="$OPENDIALER_RELAY_REF" --dry-run --skip-vault
supabase db push --remote="$OPENDIALER_RELAY_REF" --skip-vault
supabase functions deploy telnyx-webhook managed-setup callback-fallback --remote="$OPENDIALER_RELAY_REF"
```

| Pilot check | Pass condition |
|---|---|
| Import each setup file and sync its HubSpot segment | Each rep sees only their assigned test contacts and can start their first call. |
| Two simultaneous calls from the same pooled number | Each approved phone shows the shared caller ID and has two-way audio with its own rep. Record Telnyx’s concurrent channel use. |
| Both phones call the pooled number back | Each callback rings only the last confirmed rep and opens the matched prospect. Answer, decline and hang-up affect the intended call. |
| Repeat with the app paused or its window hidden | The registered app rings. A declined or missed call plays the greeting and creates one task. |
| Reach a lowered pilot outbound limit and call back | The next outbound attempt waits. Inbound rings and stays outside the counter. Local tests prove the actual 90-attempt ceiling. |
| Close the rep’s app before the callback | The caller hears the greeting and the intended rep sees one missed-call task after reconnecting. |
| Move a test contact to the other rep’s segment and call it again | The prospect sees the same number. Once the second rep’s call is confirmed, the next callback reaches that rep. |

- [ ] **R12.12 Expand the shared pool and publish the signed candidate.**
      After the pilot passes, check which application owns each approved production number and carry forward
      confirmed caller-ID bindings and callback ownership before expanding beyond the pilot.
      Use the signed release procedure in section 12A. If the rollout needs to stop, pause new pooled outbound
      calls, keep the inbound route and callback bindings available, and reconcile in-flight calls before switching profiles.

The operator completes the code, local tests, relay deployment, and any pilot preparation covered by
existing access and approved test assets. Once the live run is ready, bundle any remaining inputs into one request.
Use two approved destination phones and two HubSpot test segments. The owner chose two isolated app instances on
this Mac for the pilot. The live results identify the shared host.
The owner chooses or approves the account-owned pilot caller number and authorizes the live test. Check available
test assets and admin access before asking for anything. The signed rollout follows section 12A.

## 14. Risks

| # | Risk | Handling |
|---|---|---|
| R1 | Automated dialing can fall under state telemarketing laws. Florida is the first to check; this is from memory and unverified. | Tests dial only the owner's phone. Lawyer check before real lists. One call at a time with the rep present. |
| R2 | Voicemail detection can hang up on a real person. Jacob Pepper's call was flagged as a machine and marked gatekeeper. | `S` to stay; screening excluded; 50-call accuracy report; change the rule if false positives are high. |
| R3 | Live transcription can raise consent issues in some states, however long text is kept. Keeping it 30 days adds exposure. | Include in the lawyer check. Local only, delete control, easy to shorten. |
| R4 | Automatic promotion removes a manual check. | Models only reorder leads. Hours, attempts and opt-outs are separate hard rules. Comparison group and automatic rollback. |
| R5 | Removing the parking parameter could behave differently than the spec reads. | L9 and L10 test it on real calls in Phase 0 and Phase 4. |
| R6 | The fresh-leg reconnect could be slower than target or fail when the app is in the background. | Measured live; L11 covers background; the blocker explains; keeping a separate bridge is the fallback design. |
| R7 | DDGS can be rate limited or break. | Fallback only; quote checks protect quality; failure marks leads rather than inventing. |
| R8 | The relay runs on Supabase Free, which can pause after inactivity. | Blocker names it with the fix. |
| R9 | Migration could damage data. | Copy, never modify `local-2`; test on a copy of the real database first. |
| R10 | Building on `main` means an urgent fix may have to wait for an unfinished phase, because there is no separate 1.0.x line to patch. | Installed copies stay on the last signed release. Each commit keeps calling working, phases end green and `v1.0.2` is the rollback point. If `main` cannot ship in time, the owner decides whether to allow one short-lived fix branch from the tag. |
| R11 | Some HubSpot facts are unconfirmed: Service Key scopes, `notes_last_contacted` behavior and the lack of a documented duplicate guard. Reports on Nooks' outcome logging conflict. | R9B.7, L18, L19 and L20 settle each. Calling keeps working if any of them fails. |
| R12 | Call summaries leave the Mac once HubSpot is connected, which changes the "local only" handling in R3. | Summary only, never the transcript. HubSpot is optional. Include it in the lawyer check. |
| R13 | Numbers may reach the wrong person, and nothing has measured how often. Namesakes pass the Trestle check by design of the D18 cost decision. | `wrong_party` on real calls, T3, the 5% review trigger in section 9A and the opening question. Live tests still dial only the owner's line. |
| R14 | CSV leads, including the current call sheet, get no do-not-call check by owner decision (D23). | CRM leads are checked by RealValidito. Include unscreened CSV calling in the lawyer check before real lists. |
| R15 | The in-app waterfall spends money on every new CRM lead. | Per-list limit set before the first sync, an estimate shown first, stored answers never rebought, and a capped Treg token. |
| R16 | An opt-out on one seat reaches the others only through HubSpot, so another seat could call during the sync gap. | Written to HubSpot at once; sync before each session and every 30 minutes; owners never overlap, so the opted-out contact is not on another seat's list. Include it in the lawyer check. |
| R17 | The relay becomes shared state for the whole team: a relay defect or outage stops every seat. | Isolation tests in `supabase/tests`, named blockers, and calls already live still end. Supabase Free can pause (R8); move the relay to a paid plan before real team calling. |
| R18 | A reassigned contact is re-checked on its new seat, paying the waterfall and do-not-call check again. | Small cost, accepted. Stored answers are per seat. |

## 15. What stays out of scope

- A shared server, hosted serving, in-app logins, roles and team permissions. A team is seats (D25).
- Paid Telnyx Number Reputation.
- Claims of connect-rate or conversion lift without real measurement.
- Buying numbers, placing billable calls outside the test protocol, or installing on this Mac without an explicit OK.
- Fit rules and still-at-company checks, which the CRM stage owns.
- Number checks or do-not-call checks on CSV leads (D19, D23).
- Reverse lookups and other paid namesake checks (section 9A).
- HubSpot webhooks, deals, tasks, recordings and field write-back beyond `phone_status` and opt-outs.
- A Nooks adapter or a universal CRM layer.

## Status

- November 6, 2026: plan written after a read-only audit of source `730a80d` and the installed 1.0.1 workspace. No
  product code changed, and no calls were placed. Next: Phase 0, starting with R0.1.
- November 6, 2026: added one intake path, HubSpot, row-signal fixes, non-Treg number tests and live checks L14 to L20
  after a read-only lead data audit. Fit and job checks moved to the CRM. The number check and Treg testing stay held
  (section 9A, D18). Still no product code changed and no calls placed. Next is unchanged: Phase 0, starting with R0.1.
- November 6, 2026: ran the agreed Treg number test on 63 real rows for $5.32 (section 9A). The Trestle check passed and
  joins Phase 9, which settles D18. The live-line check stays. Blitz could not be tested because Treg's account is at its
  fair-use limit. Telnyx was not run. No product code changed and no calls placed. Next is unchanged: Phase 0, starting
  with R0.1.
- November 6, 2026: ran the waterfall without Blitz on 100 new contacts for $5.19; 82 confirmed (section 9A). No product code
  changed and no calls placed. Next is unchanged: Phase 0, starting with R0.1.
- November 6, 2026: owner locked the waterfall with Wiza last and dropped the live-line and reverse lookups (D18, D19). Built
  `run_waterfall.py` outside this repo: 20 offline tests pass, and a dry run on a copy of the ledger called only Wiza, on the 17
  unresolved pilot contacts, and changed no accepted number. It has not made a paid call, so Wiza's lift on this list is still
  an estimate. No product code changed and no calls placed. Next is unchanged: Phase 0, starting with R0.1.
- November 6, 2026: owner set the build rules: everything on `main`, one worker in one thread, CI/CD as part of the build (D14,
  D20, D21, section 12A). Plan text only; the workflows are not changed yet. Next is unchanged: Phase 0, starting with R0.1.
- November 7, 2026: owner asked for local CI/CD for fast iteration (D22). Timed every check on this Mac: backend 111 seconds,
  other unit checks under 10 seconds, and the browser suite cannot run locally (26 minutes and 40 failures once the hosted
  server is removed). Added R0.0, R1.2a and the local tiers in section 12A; Phase 0 grows by a quarter day. Plan text only. Next:
  Phase 0, starting with R0.0.
- November 7, 2026: owner moved the waterfall inside the app for CRM leads, with no checks on CSV leads (D19), and added a
  RealValidito do-not-call check on CRM leads (D23, API read from its public docs). Number work moved to Phase 9B (R9B.14 to
  R9B.20); estimate now 19.5 to 25 days. Owner asked for the whole plan to be built end to end in this thread. Next: commit
  the plan, then R0.0.
- November 7, 2026: building end to end in this thread. Done: R0.0 to R0.13a (local check loop, hotfix fixes, release
  gate, version 1.0.2), Phase 1 except R1.1 (replay carrier, variants, property test, WebKit suite locally and on a
  macOS CI job, layout audit, rules and caps as warnings, baselines), and R2.1 (hosted mode, PostgreSQL, deploy and
  Windows removed). Waiting on the owner: R0.14 publish and install of 1.0.2 (the draft is built from the first green
  commit after R2.1), R0.15 and the other live calls, R1.1 (tag `v1.0.2` once published). Next: the 1.0.2 draft, then
  R2.2.
- November 7, 2026: R2.2 done and pushed (`5387421`). Signed 1.0.2 draft built by the gated release from `b8e2cd0`
  (Verify passed; Mac only; `latest.json`, archive, signature, disk image). Not published or installed: both wait for
  the owner. Next: R2.3. Its experiments module also serves coaching strategies (R2.4) and the StatML comparison group
  (kept), so remove comparisons, trials, drift and variants while keeping `assign_arm` and the holdout path.
- November 7, 2026: Phase 2 done and pushed (`0610378`, CI green on each push). Routes 162 to 117, tables 57 to 53,
  backend 39,428 to 34,716 lines (target missed; see Phase 2 result). Deferred on purpose, with the step that owns each:
  password sign-in and roles (R3.4), the direct webhook route (R4.7), coaching library and scorecard internals (R10.1,
  R10.2), saved trial state in projections (R8.4 to R8.6). Still waiting on the owner: publish and install of the 1.0.2
  draft, live calls (R0.15 and later), RealValidito key, HubSpot key. Next: Phase 3, starting with R3.1.
- November 7, 2026: Phase 3 started under D24. R3.1, R3.3 and R3.10 done (`653db0b`). Found a launch defect from R2.1:
  the desktop shell still passed `--host` and `--mode`, which the backend no longer accepts, so the packaged backend
  would exit at launch. The 1.0.2 draft built from `b8e2cd0` has this defect and must not be published; it needs a
  rebuild from a green commit after the fix. A new test reads the shell's arguments from `lib.rs` and parses them with
  the backend's own parser. Next: R3.7 to R3.9 (migration) and R3.11.
- November 7, 2026: Phase 3's own steps are done: `Ctx` and the package layout, the import-direction test, strict
  rules in the new packages, the `local-2` to `local-3` migration (tested on a copy of the real store) and lead fields.
  Installed data is untouched; it moves only when the owner installs a release containing this. Next: Phase 4, R4.1.
- November 8, 2026: Phase 4's automated work is done and pushed: one transition table with a limit and exit per
  state, pure event steps, one `blockers()` gate, the audio leg in `calling/` with a once-per-session credential
  check, carrier outcomes saved at hang-up, an automatic recovery pass that polls quiet live legs and ends overrun
  states, backend auto-restart, keep-awake, a call timeline in Review, crash tests at seven steps and outage tests.
  Found and fixed: a call stuck ringing waited two hours for recovery, and every backend restart was refused by the
  relay for up to 45 seconds. Carried forward: the dial code move (Phase 9). Waiting on the owner: live checks R4.15
  (L1, L2, L3, L9, L10, L13) and the 1.0.2 rebuild. Next: Phase 5, R5.1.
- November 8, 2026: Phases 5 to 7 largely done and pushed. The Dialer is one calling loop in three fixed regions:
  Start calling dials by itself, Space skips, Esc hangs up, keys 1 to 6 save, and saved or skipped calls count down
  to the next. Voicemail is skipped at detection, never before 7 seconds. Test sessions dial only the test phone.
  Settings opens with the calling status and eleven rows that turn green only after a real check. Learning skips
  test calls and uses `meeting_booked` as its goal. The browser suite was rewritten for the new screen and caught
  ordering defects and a goal mismatch, all fixed. Open: CI browser retries are back to 1 while a click flake under
  load is found (R5.12); the older Settings cards still need folding into the rows (R7.2); the rest of Phase 8.
  Waiting on the owner: R5.13, R6.7, R7.8 read-through, live checks, the 1.0.2 rebuild and keys. Next: R8.3.
- November 8, 2026: Phase 8 done. StatML learns from typed lead, number and trigger features, skips test calls,
  promotes a model whose out-of-time review passes, rolls it back when the comparison group wins, trains at most
  nightly and shows what it has learned in Review. The synthetic test found the review was a coin flip on small
  calibration windows; it now finds a planted pattern in at least 7 of 8 seeds and promotes noise in about 1 of 20.
  Estimates are counted straight from calls, and the projection engine and its saved report views are gone. The
  frozen trainer is checked in CI by a real training run. Real lift stays an open gate until enough real calls exist.
  Carried forward: R4.5 (Phase 9), R5.2, R5.12, R7.2, R7.8. Next: Phase 9, R9.1.
- November 8, 2026: owner chose seats for team calling (D25) and a shared number pool (D26); section 9C and Phase 9C
  added, with live checks L21 to L24, risks R16 to R18 and new owner inputs. The Telnyx account was read in the portal:
  Verified, every check met including KYC. Estimate now 22 to 28.5 days. Plan text only. Not yet pushed: `ca1eebb`
  (R8.4) and `bc06452` (R8.10). Uncommitted: `leads/intake.py` and `prep/criteria.py` with their tests in the
  scratchpad. Next: push, then Phase 9, R9.1.
- November 8, 2026 (later): owner brought Windows back (D2 now Mac and Windows) with seats and the pool unchanged; Phase
  9D and live checks L25 to L27 added; pool sized at 15 numbers for 3,000 dials a week. Estimate 25 to 33.5 days. Plan
  text only. Next: push, then Phase 9, R9.1.
- November 8, 2026: Phase 9 and 9B code done. Leads come through one column map; research reports a trigger that code
  checks against the script's `# Triggers` list, and a bridge line reviewed as labeled inference; the Dialer and Leads
  show both, and number holds by name. The DeepSeek fallback with local search takes over a failing ChatGPT thread and
  loads in the frozen sidecar. HubSpot lists sync by owner into leads, CRM numbers run the ported waterfall through Treg
  and a RealValidito do-not-call check, calls and opt-outs write back, and a list unsynced for a day stops dialing its
  leads. All of it is tested over recorded-shape answers only: no HubSpot, Treg or RealValidito key has been used.
  T4 and T5 recorded (section 9A). Waiting on the owner: keys, R9.14, R9.15, R9.19, R9B.1, R9B.7, R9B.13, R9B.20.
  R9C.2, R9C.5 and R9C.6 came with it. Next: Phase 9C, R9C.4.
- November 8, 2026: Phase 9C code done: seats by HubSpot owner, the shared pool in the relay with atomic leases and
  team-wide caps, callbacks to the last caller, a named blocker for Telnyx's account call limit, per-seat research
  route, and seat setup from the owner's computer. Waiting on the owner: applying the relay migration to the live
  project, R9C.1, the pool numbers, and live checks L21 to L24. Next: Phase 9D (Windows), R9D.1.
- November 8, 2026: Phase 9D's code is done and CI is green on Mac, Linux, Windows and Chromium (`18d639f`). Windows
  ships again as signed-for-update NSIS and MSI installers; Authenticode waits for the owner's signing route (R9D.4).
  A Dialer loop found by the Chromium run is fixed: pausing re-paused itself hundreds of times a second and hid the
  reason calling stopped. Still open: Phase 10 (R10.1 to R10.11; R10.5 changes the speech privacy boundary and is
  not started), R3.2, R3.4 to R3.6 and R4.5 (structure), R5.2, R5.12 (one import flake under full load remains) and
  R7.2. Waiting on the owner: keys, the relay migration, pool numbers, a Windows PC and signing route, and the live
  checks. Next: Phase 10, R10.1.
- November 9, 2026: released 1.0.3, 1.0.4 and 1.0.5. Fixed the whole-app redraw on every background event, a lost Pause
  click (Start and Pause now decide the screen at once), and a relay delivery gap that kept calling blocked after days
  closed. Review opens with what needs the rep; learning starts at 40 calls. Owner direction: no problem in any area
  should stop calling unannounced; added G10 and Phase 9E, which comes before the rest of Phase 10. HubSpot key added
  and checked by the owner. Next: Phase 9E.
- November 9, 2026 (later): released 1.0.6 (test call as one direct action, the Dialer names why no one is callable,
  HubSpot checked live). HubSpot and Treg checked against the owner's accounts on one test contact
  (`docs/evidence/rebuild/2026-11-09-hubspot-treg-live.md`): sync, waterfall ($0.03), write-back, call logging, summary
  and opt-out all work; a repeat call log now finds the first record. Phase 9E R9E.2 to R9E.6 done; R10.6 done; Script
  page shows its save state and folds usage tools. Next: Settings (R7.2), Phase 10 helper and after-call, R9E.7.
- November 9, 2026: next steps, in order. (1) R10.5 first: transcript text is held only in the in-memory raw store for
  minutes (`privacy.py` `transcript_rows`, `self.raw`), so no summary can read it later. Persist each final segment's
  text and speaker on its `transcript_segments` row with a 30-day expiry (D7), read it back in `transcript_rows`, purge
  expired rows in the maintenance tick, add `DELETE /v1/calls/{id}/transcript`, then retire the raw store. (2) R10.2:
  `app/aftercall/summary.py`, one `InferenceClient.complete()` over the whole transcript (no 32-segment page) through
  OpenRouter's no-retention route (`chat_agent.ROUTING`; add `extra_body` to `complete()`), returning summary, suggested
  outcome and next step, each field validated on its own (R10.3). The current post-call path never runs in production
  because `postcall_zdr_verified` is unset, so live calls get no summary today. Then point `suggested_outcome` reads at
  the new record, add the summary to the HubSpot call record, and stop enqueuing `conversation_finalize` and review.
  (3) R10.1 helper, R7.2 Settings, R9E.7 relay module, R10.10 layout audit, R10.11 docs, then 2.0.
- November 9, 2026 (evening): released 1.0.7 (watchdog and Reconnect, readiness before Start, Leads and Lists on one page,
  Script save state) and built 1.0.8 (after-call summary, 30-day transcripts, steady Leads). R10.2, R10.3, R10.5, R10.8 and
  R10.10 done. Next: R10.1 (retire the old finalize, review and interpretation path now that the summary replaces it),
  R10.4, R7.2, R9E.7, R10.11 docs, then 2.0.
- November 9, 2026 (night): R10.11 done; Settings edit cards start folded. Open and not started by the builder:
  R10.1 and R10.4 (retire the old finalize, review and interpretation path; the new summary already replaces what the
  rep sees), R9E.7 (relay as a plain module; its exits are now tested and watched), R9E.8 beyond Start and Pause, and the
  structural steps R3.2, R3.4 to R3.6 and R4.5. Owner-gated as before: live checks (L1 to L27), the lawyer check, a
  Windows PC and signing, a second phone, the relay pool migration and pool numbers, the RealValidito key.
- November 9, 2026 (late): R10.1, R10.4, R9E.8, R7.2, R10.7, R10.9 and R3.6 done; 1.0.9 cut. The live helper is
  tips and private help only (`helper/live.py`); `coaching/`, `postcall/`, the playbook and 10 tables are gone, and
  Review, the next script and the brief read the after-call summary. Every Dialer key shows at once over any re-read
  (`withIntent`), checked over every input. Found and fixed: an outcome pressed while a call was still ending was
  dropped; on Windows (Chromium) the Dialer ignored Esc and 1 to 6 after any click. Settings is rows only; each
  number is one row with Pause or Resume; one writer, so the last save wins. Backend 38,623 to about 32,400 lines.
  Owner-gated as before.
- November 9, 2026 (night): 1.0.9 published and installed on this Mac; 1.0.10 built from the next green commit. R5.12
  done (CI runs every browser test once). Found in the installed app and fixed: keys saved through the old cards could
  not be checked without retyping them; follow-ups were long forms, now one row each. Found by review and fixed: a
  live helper turn that never reached OpenRouter kept its budget hold, and the helper never saw which script section
  the rep was on during a call. Found by Chromium CI and fixed: a Pause pressed while the next lead was loading still
  dialed that lead, and a Pause pressed while audio was connecting still reserved a lead and could leave the server
  thinking the rep was ready. Open and not started: R3.2 (mixins to modules), R3.4 (users, roles, password sign-in
  and sessions; a same-workspace teammate is still a code path, though the UI no longer creates one), R3.5 (typed core
  records), R4.5 (dial code into `calling/dial.py`), R5.2 (global element CSS) and R9E.1 (one calling model; its laws
  are checked piecewise today). These change structure, not behavior; each is a multi-day rewrite.
- November 10, 2026: owner direction: finish to 2.0; one user only (D27); test calls to 203-821-1101 are allowed as
  needed; HubSpot test records may be created and deleted. Done and pushed: seats, the pool and "Calls for" removed;
  R3.4 (no roles, passwords, sign-out or sessions table); R3.2 (mixins to module functions behind `app/ops.py`); two
  stale-answer races in Settings fixed; tests repeat clicks that WebKit drops under load. Then R4.5 (dial code in
  `calling/dial.py` with typed reads), R3.5 (typed views; forbid only where a package owns the record), R5.2 (element
  rules in `@layer base`, screenshots unchanged) and R9E.1 (`calling/decide.py` with an exhaustive law check). Every
  software item in the plan is now done. Next: the live matrix on this Mac (needs the screen unlocked and the owner
  answering 203-821-1101), HubSpot L14 to L19 on a test contact, then signed 2.0.
- November 10, 2026 (night): live checks on two 2.0.0 candidates (`docs/evidence/rebuild/2026-11-10-live-calls.md`):
  L1 and L2 passed on the owner's line. Found live and fixed with tests: a microphone request with no time limit, a
  held test lead never offered, a saved test call that counted down into the real lists and lost its test mark, and
  no transcript on auto-dialed calls. The transcript's cause was a dead audio leg from November 6 reused because the
  store still called it answered; a leg is now reused only if this run made it and the carrier says it is live, and
  the Dialer dials only once its own audio client holds the leg. The live helper drops script tracking for a
  numbered script part named by each tip. Every handled failure now logs a line, and every event-driven re-read
  shows only the newest answer (CI caught a removed list shown as Paused). Next: candidate 3 from the green head,
  install, a live call that proves transcript, helper and the rep's voice, then publish 2.0.0. Owner-gated: HubSpot
  L14 to L18 need a list in the owner's HubSpot (the key cannot create one), Windows L25 to L27 need a PC and signing.
- November 10, 2026 (morning): owner approved Phase 11 after live findings: callbacks never reached the app, a
  number stuck as purchased, StatML archived for stage drop-off, forwarding deferred, voicemail skip kept (D29 to D32).
  HubSpot: the key reaches portal 51457381; one contact of 4,759 has a number; segment "testing" syncs on the real
  list read (L14). Next: R11.2.
- November 10, 2026 (afternoon): R11.2 to R11.8 built and tested: pending numbers re-read from Telnyx (and a refresh
  never parks a still-ordering number); callbacks ring the open app with a tone, Dock bounce, Answer and Decline, for
  any caller; forwarding hidden; StatML archived (3,100 lines, the trainer sidecar and its CI and release steps
  removed; the queue is callbacks, never called, longest waiting); stage drop-off in Review from the after-call
  summary; test calls to the owner's HubSpot contact written; a fixed call-screener tip. CI green on the archive
  (`1b1d4d4`). Next: candidate 4, install, live sessions (R11.9), publish.
- November 11, 2026: the owner set shared calling as 2.1 scope (D33), with sticky caller IDs and up to 90 outbound
  attempts per number. The two-profile checks passed on a disposable relay with replayed calls and in the Mac app.
  [Local evidence](evidence/rebuild/2026-11-11-2.1-local.md).
- November 11, 2026: recorded the one-application rollout in Phase 12. The read-only Supabase check confirmed the
  October migration history, the missing pool schema and the older receiver. The next implementation steps are
  R12.5 and R12.6, aligning the migration filenames and fixing first-call audio for a new rep.
- November 11, 2026: R12.5 to R12.7 and R12.9 implemented. R12.8 profile issuance and receiver exercised on
  disposable Supabase. R12.10 exercised with two packaged 2.1.0 apps and signed fixtures on this Mac.
  [Verification receipt](evidence/rebuild/2026-11-11-2.1-shared-application.md).
- November 11, 2026: the owner approved the in-place rollout. Four migrations and three functions were deployed to
  the existing project. The existing Mac stayed connected, and two temporary hosted profiles passed signed callback
  routing at the cap. Temporary records were removed. The dedicated pilot Call Control application and two
  incoming-only WebRTC credentials are ready. R12.11 still needs an approved caller ID, both test phones and
  segments, and the worker's secrets and schedule. R12.12 follows the pilot and signed release gate.
- November 11, 2026: the owner approved 1101 as the first recipient. On the installed 2.0.0 Mac, the audio leg timed
  out on the first start. After relaunch, one test call reached the approved line and saved voicemail. See the
  [standalone Mac test](evidence/rebuild/2026-11-11-2.1-shared-application.md#standalone-mac-test-after-deployment).
  R12.11 still needs a second phone, an approved caller ID, the segments and the worker setup.
- November 11, 2026: verified the existing HubSpot test contact and fixed the handling of stored Phone and Mobile
  Phone values, number changes during calling, and phone write-back. The two-profile replay test used CRM leads
  through the normal queue. A rollback-only check on the existing Supabase relay confirmed separate callback inboxes
  at the outbound cap. [HubSpot phone verification](evidence/rebuild/2026-11-11-2.1-shared-application.md#hubspot-numbers-already-on-contacts).
- November 11, 2026: automatic tips and private help now show the exact response chosen from the prepared
  script. Phone screening uses the prepared Screening line. Backend acceptance tests and the local WebKit
  Dialer replay passed.
- November 11, 2026: built the signed 2.1 draft from `b4e867a` and installed it over the Mac's 2.0.0 app.
  The original workspace reopened with its leads, call history and bindings. The second app uses the same
  commit with a separate local bundle identifier. Both instances imported their assigned HubSpot test contacts
  and held separate leases and audio credentials on the existing Telnyx route. The hosted pool uses all 11
  existing number assignments and a 90-attempt cap. Both outbound starts waited for microphone access.
  The relay received zero audio or prospect dials. The closed-app worker still needs its Edge secrets and schedule.
  [Signed pilot receipt](evidence/rebuild/2026-11-11-2.1-shared-application.md#signed-pilot-on-two-mac-instances).
- November 11, 2026: macOS microphone access cleared and the hosted relay matched a separate audio leg to each
  rep. Neither prospect call submitted: Test mode held the saved HubSpot phones for prospect screening. The
  regression and fix pin Test mode to the saved contact, preserve regular screening, and stop pending test
  dials when the session ends. Next: install the corrected signed candidate and run R12.11.
- November 11, 2026: fixed the Mac callback browser test so the app receives a live event while Settings is
  open. Local checks and Verify passed on `7cdbe3e`. Installed the corrected signed 2.1.0 draft over the primary
  app and preserved its workspace. Rep B reached 8547 from the shared number and saved voicemail. macOS
  rejected the primary app's previous microphone grant for the updated code signature, and the Mac locked
  before the grant could be refreshed. The human audio, overlap and return-call checks in R12.11 remain open.
  [Live receipt](evidence/rebuild/2026-11-11-2.1-shared-application.md#corrected-signed-build-and-first-shared-pool-call).
- November 12, 2026: refreshed the primary app's microphone grant in System Settings. Both approved
  calls reached Telnyx from the same pooled number. The first 1101 callback matched the primary prospect
  and went to the greeting. The retry rang only the primary app, opened the correct prospect and was
  answered there. The 8547 phone was unavailable for its return call. R12.11 stays open.
  [Live callback receipt](evidence/rebuild/2026-11-11-2.1-shared-application.md#november-12-microphone-recovery-and-live-callbacks).
- November 13, 2026: added the Ubuntu 24.04 LTS x86-64 desktop package to 2.1.3 (D2). Verify installs the `.deb`,
  opens its window and checks the frozen backend on a fresh workspace. The signed release includes the Linux package
  and its update signature. Linux carrier audio remains a separate live check.
- November 13, 2026: added explicit file pickers for calling setup, Codex login, Markdown scripts and CSV leads.
  The Numbers table refreshes when the relay mirrors pool numbers, and connection status counts the shared pool.
  Supabase readback confirmed Tanush, Taegan and Parth each have one assigned segment and see the existing 11-number
  pool. Personalized scripts start per list (D34). The rep ZIPs still need RealValidito credentials before HubSpot
  calling, and each rep can connect their own ChatGPT login from Settings.
- November 13, 2026: the 2.1.2 Mac app found public release 2.1.4 through its native updater. For 2.1.5, update
  checks display their result and a direct release link. The live helper coalesces finalized prospect segments across
  a one-second pause, discards selections superseded by new speech, and uses the last delivered script section.
- November 13, 2026: test calls use a dedicated Dialer action for the saved test phone. Prospect hours, retry spacing,
  outreach holds, and stale CRM data do not gate it. The carrier's own call duration ceiling still applies. Either
  a carrier or rep outcome ends the test session and pauses calling. Removed the internal Call by date from Leads.
