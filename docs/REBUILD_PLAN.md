# OpenDialer rebuild plan (active)

Approved by the owner on November 6, 2026. This is the active plan. Read it in full on every continuation or context
compaction, including the Status section at the end, which is the current checkpoint.

It supersedes the "every feature stays" and "both modes" rules of [LEAN_PLAN.md](LEAN_PLAN.md), the manual-dial rule of
[CALLING_COMPLETION_PLAN.md](CALLING_COMPLETION_PLAN.md) and the "No DDGS" decision of
[CAMPAIGN_PREP_PLAN.md](CAMPAIGN_PREP_PLAN.md). Those files remain history for what a feature does.

## 1. TLDR

- Make OpenDialer a small, single-user Mac dialer that is simple enough for bugs to show up in the code and tests.
- Keep the working integrations: Telnyx, speech, ChatGPT agent, relay and signed releases.
- Cut about 60% of the code, then rebuild the call core, Dialer, Settings, StatML learning and research criteria.
- Work one module at a time. A calling module is finished only when live calls to the owner's test line pass and the
  owner signs off.
- Take leads from CSV or HubSpot through one intake path and log calls back to HubSpot. The CRM owns fit and the
  person; this app owns number accuracy. CRM leads get the number waterfall and a RealValidito do-not-call check inside the
  app; CSV leads are taken as they are.
- Build everything on `main`, with one worker in one thread. A local check loop gives answers in seconds while working and
  must pass in full before every push; GitHub CI confirms on a clean machine (section 12A).
- Estimate: backend 39,300 to about 17,100 lines, frontend 22,100 to about 6,600 lines, 19.5 to 25 working days, plus
  about 5 owner sittings of 30 to 60 minutes.

**What this plan can and cannot promise.** Failure-proof is not possible, because Telnyx, the network and the Mac can
always fail. Failure-resistant is possible: a failure stays contained, says what happened, recovers by itself where it
can and does no harm. Section 5 lists those guarantees and how each is tested.

## 2. Decisions

| # | Decision | Status |
|---|---|---|
| D1 | Remove hosted mode, PostgreSQL, teams and roles. One user, one Mac, one SQLite file. | Approved |
| D2 | Mac only. The Windows build and its release path go. | Approved default |
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
| D19 | The number waterfall is LeadOcean, AI Ark, Tomba, then Wiza, with Trestle on every number. For leads pulled from the CRM it runs inside the app. Leads that come from outside the app (CSV) are taken as they are: no Trestle, no finder and no other check. The outside runner stays for building CSV lists. Blitz joins first after its reset. | Owner decision, November 7; replaces "runs outside the app" |
| D23 | Leads pulled from the CRM get a RealValidito do-not-call check (federal, state and TCPA litigator) before they can be dialed. Any hit is suppressed. CSV leads are not checked. | Owner decision, November 7; replaces the DoNotCallDNC key |

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

| Outage | Behavior after the rebuild |
|---|---|
| Telnyx API | Dial fails with a plain reason; retry after confirmation; a live call can still end through WebRTC |
| Relay or Realtime | Realtime is only a speed-up. New dials stop only if HTTP delivery fails. During a call, carrier status is polled and the WebRTC leg state is watched |
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

- **CRM leads, inside the app.** `leads/waterfall.py` runs these steps on every lead pulled from HubSpot, as a job after sync.
  It ports the rules and the 20 offline tests from the outside runner.
- **CSV leads, outside the app.** `phone-waterfall/sets/agite-us-leadership-noblitz-2026-11-06/run_waterfall.py` builds the
  CSV. The app imports those rows as they are and runs no Trestle, finder or other check on them. It still reads a mapped
  `DEAD` column, which is free.

1. A number already on the row or in the CRM goes straight to step 3.
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

One account, one user, barebones. HubSpot picks who is called and the app picks the order. Calling never depends on
HubSpot.

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

Each session records a receipt under `docs/evidence/rebuild/`: scenario, version, pass or fail, timing and notes. It also
answers whether the hourly carrier cap applies and whether the automated notice plays.

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
  RELEASE_NOTES, this plan and `evidence/`. `NUMBER_CONNECT_PLAN.md` stays open for its traffic ramp and measurement
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
| 10 | Helper, summary, remaining pages, docs, 2.0 | 1.5 to 2 |
| | **Total** | **19.5 to 25** |

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
| Release | Check that Verify passed on this exact commit, then build signed Mac artifacts and a draft release | Manual start, on the owner's OK |
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
- [ ] R0.14 Full CI, then signed 1.0.2 on the owner's OK. Tag `v1.0.2` as the rollback point.
- [ ] R0.15 Live L1, L2, L3, L9 and one more answered call; receipt under `docs/evidence/rebuild/`. L9 confirms that
      removing the parking parameter ends the prospect's leg when the rep's is lost.

Done when: 5 live calls need no Go ready and produce no false block. **Owner sitting 1.**

### Phase 1: guardrails and CI

Goal: today's failures become failing tests before any structure changes.

- [ ] R1.1 Confirm `main` is at the `v1.0.2` tag with CI passing, and record the CI time as the baseline.
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
- [ ] R3.2 Replace mixins with module functions. `Dialer`, `Application` and `Service` go.
- [x] R3.3 Add an import-direction test. Done November 7: `tests/test_imports.py` ranks the packages, bans any import
      from a new package into legacy code, and lists the 9 existing edges with the step that removes each; the list can
      only shrink.
- [ ] R3.4 Remove `p`, tenant and seller scoping, roles, password login and sessions; keep the local token.
- [ ] R3.5 Add Pydantic models for Call, Lead, Number, Task, Reservation, Command and Script with `extra="forbid"`.
- [ ] R3.6 Remove edit-version conflict checks now that there is one writer; keep the idempotent dial and hang-up keys.
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
- [ ] R4.5 `calling/dial.py`: keep durable dial and hang-up records and read-only reconcile; `save_disposition` under
      120 lines.
      In progress: `save_disposition` is 66 lines (November 8). The dial code still sits in `service.py`.
- [x] R4.6 `calling/recovery.py`: one automatic pass at startup and every minute; open calls, uncertain dials, stuck
      hang-ups. Needs attention lists only what software cannot settle.
      Done November 8: every call records when it entered its state; `calling/recovery.py` decides when each
      unfinished call is due and what the pass does: read a quiet live leg, hang up a call that overran its state, end a
      dial that was never sent, mark an unanswered one unconfirmed, and end what the carrier can no longer settle with a
      review flag. Before this, a call stuck ringing waited two hours. Needs attention itself is R7.6.
- [ ] R4.7 `calling/relay.py`: gate on HTTP exchange success; Realtime only wakes it; handle restart lease conflicts
      without a block.
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
- [ ] R4.11 Keep the Mac awake while Ready.
- [ ] R4.12 Call timeline visible in Review; one log file with rotation.
- [ ] R4.13 Crash tests: kill at each dial and hang-up step; no double dial, no lost call.
- [ ] R4.14 Fault injection for every row in the outage table in section 5.
- [ ] R4.15 Live L1, L2, L3, L9, L10, L13.

Done when: all automated guarantees pass and the live checks pass. **Owner sitting 2.**

### Phase 5: UI kit and Dialer

Goal: nothing moves, nothing gets covered, and the rep always knows what is happening.

- [ ] R5.1 Design tokens and eight components: Button, Field, Select, Check, Status, Panel, Toast, Table.
- [ ] R5.2 Replace global element CSS; add breakpoints; set a real minimum window behavior.
- [ ] R5.3 Three fixed regions: status bar, script with its own scroll, action bar. Reserved space for every phase.
- [ ] R5.4 `screen.ts`: pure phase function with a `never` check; table tests for every phase.
- [ ] R5.5 One state stream: `useCallState()`; remove `reservation`, `call`, `packet` and generation logic from local state.
- [ ] R5.6 Background events do not refresh the call screen.
- [ ] R5.7 Outcome keys `1` to `6`, with `wrong_party` among them; one key saves; Add detail holds note, callback time and
      more outcomes.
- [ ] R5.8 Hang-up display with sound; countdown; clear finished lead after save.
- [ ] R5.9 Move Dial from and list choice out of the call view; remove Get next lead, View next lead, Start call,
      Pause after call and the voicemail checkbox.
- [ ] R5.10 Error boundaries per region so the action bar survives a crash elsewhere.
- [ ] R5.11 Render real script Markdown, including tables, without overflow; test with the owner's master script.
- [ ] R5.12 Layout audit passes for every phase, using long names and the real script.
      Then set the browser suite's retries to 0 in CI, so any flake fails (one retry since R2.1).
- [ ] R5.13 Owner uses the Dialer on test calls and reports anything confusing.

Done when: the audit passes and the owner confirms it reads clearly.

### Phase 6: auto-dial and voicemail

Goal: the loop runs with almost no decisions.

- [ ] R6.1 Failing tests for the behavior table in section 6.
- [ ] R6.2 Auto-advance after Saved (3 seconds) and Skipped (1.5 seconds), with Space to go now and `P` to pause.
- [ ] R6.3 Voicemail: on detection, hang up at the later of detection and 7 seconds; final attempt holds; `S` stays;
      screening is not skipped.
- [ ] R6.4 Log detection against what really happened; report after 50 calls.
- [ ] R6.5 Auto-dial stop conditions, each with a plain message.
- [ ] R6.6 Test-line allowlist enforced in code during test sessions.
- [ ] R6.7 Live L3 to L8, L11 and L12.

Done when: a 20-call run passes. **Owner sitting 3.** Real lists wait for the lawyer check in R1.

### Phase 7: Settings

Goal: plainly what to add and where to get it.

- [ ] R7.1 Failing tests: a row is green only after its real check; a failed check shows the reason.
- [ ] R7.2 Build the page in section 7 from `blockers()`.
- [ ] R7.3 Check on save for each key; rows show what each powers and link to where to get it.
- [ ] R7.4 Connection reconnects by itself; the setup-file import appears only when disconnected.
- [ ] R7.5 Test call button to the saved test phone.
- [ ] R7.6 Advanced section; Needs attention list.
- [ ] R7.7 Confirm the Telnyx and OpenRouter links land on the right screens.
- [ ] R7.8 Fresh-install walkthrough on an empty `local-3` copy.

Done when: the owner can read the page without explanation.

### Phase 8: StatML

Goal: it can learn, and you can see that it is learning.

- [ ] R8.1 Failing tests: test calls never enter datasets; goal `meeting_booked` labels at wrap-up.
- [ ] R8.2 Add `is_test`; clean existing rows in the migration.
- [ ] R8.3 Add features from section 8, bumping the feature schema version; keep old rows usable.
- [ ] R8.4 Compute estimates directly from calls; remove the remaining projection machinery.
- [ ] R8.5 Automatic promotion from the existing out-of-time review.
- [ ] R8.6 Automatic rollback using the 10% comparison group; record each promotion and rollback.
- [ ] R8.7 Training schedule from section 8; no UI churn.
- [ ] R8.8 "What it has learned" card in Review.
- [ ] R8.9 Synthetic end-to-end test: a planted pattern in 600 calls gets learned, promoted, used and rolled back when it
      stops working.
- [ ] R8.10 Package the trainer sidecar on Mac and verify it runs, with that check in CI.

Done when: the synthetic test passes. Real lift stays an open gate until enough real calls exist.

### Phase 9: research and fallback agent

Goal: criteria are enforced in code, and research keeps working if ChatGPT is unavailable.

- [ ] R9.1 Seed `# Triggers` from the script's `# Before you dial`; the owner confirms windows and strengths.
- [ ] R9.2 Failing tests: out-of-window, unlisted and unquoted triggers are rejected; a `row_only` signal is capped at C; a
      `page` signal without a quote is rejected; one record through CSV and through HubSpot gives the same Lead; signals with
      mismatched counts are never paired by position; a 300-property record keeps its mapped signal columns; bookkeeping
      columns never reach a model; a `DEAD` number is not dialable.
- [ ] R9.3 One intake path and column map, with the owner's rows as the test case: delimiter per column, mapping before size
      limits.
- [ ] R9.4 `# Triggers` section in the parser and script editor.
- [ ] R9.5 Fixed lead record with code checks.
- [ ] R9.6 Agent instructions: row signals first; then search; decide; write.
- [ ] R9.7 Review separates facts from the labeled bridge.
- [ ] R9.8 Show trigger, strength and evidence in the Dialer and Leads, including the "from the list, unverified" label.
- [ ] R9.9 Feed trigger fields to StatML and to first-call order.
- [ ] R9.10 `providers/chat_agent.py` from the `InferenceClient` chat loop, with `web_search` and `read_page`.
- [ ] R9.11 Add `ddgs` and confirm it works in the signed sidecar, with an import check in CI.
- [ ] R9.12 Fallback on `PlanLimit`, login failure or repeated timeout; store the route on each record.
- [ ] R9.13 Force a ChatGPT failure and confirm identical output shape.
- [ ] R9.14 Re-prepare 20 real leads on each route; the owner judges quality; measure cost, coverage and drop rate.
- [ ] R9.15 After the owner's OK, re-prepare active leads.
- [ ] R9.16 Import reads a mapped live-status column; `DEAD` is not dialed and the lead says why.
- [ ] R9.17 Keep number source per lead; feed it to StatML and the learning card.
- [ ] R9.18 Run T4 and T5 and record the results in section 9A.
- [ ] R9.19 Run T1 after the owner OKs using the saved Telnyx key; run T2 only if T1 is useful and the owner approves.
- [x] R9.20 Record the Treg testing result in section 9A and settle D18.
- [ ] R9.21 Failing test: a CSV import makes no Treg, Trestle or RealValidito request, and its leads show "Not checked (CSV)".
- [ ] R9.22 Number status and its reason show in Leads and the Dialer; auto-dial skips Conflict, Bad and No valid number; the rep
      can dial a Conflict by hand. The number-check work itself is in Phase 9B, because it runs only on CRM leads.
- [ ] R9.25 The learning card shows wrong-party rate by number status as well as number source (T3).

Done when: no lead claims a trigger that fails the criteria, and the owner accepts the 20-lead sample.

### Phase 9B: HubSpot

Goal: HubSpot picks who is called and every call lands on the contact. Calling never depends on it.

Tests first:

- [ ] R9B.1 Capture recorded HubSpot responses from the owner's account, with the test contact only: list members, contacts,
      companies, dispositions, call create, errors and rate limits. These are test fixtures; the product has no simulated
      HubSpot.
- [ ] R9B.2 Failing sync tests: paging; one contact appears once; syncing again changes nothing; HubSpot edits update the row
      without resetting attempts; a removed contact stops being dialed and keeps its history; no number is not dialable; an
      invalid phone is reported.
- [ ] R9B.3 Failing opt-out tests: a sync can add a suppression and never remove one; a local opt-out writes back.
- [ ] R9B.4 Failing logging tests: every outcome maps, or setup says which does not; a retry after an uncertain write leaves
      one record; rate limits back off; a CSV lead is created only on its first connected call and never for a no-answer; the
      summary is sent and the transcript never is; test-session calls are not written unless the contact is on the test list.
- [ ] R9B.5 Fault injection: HubSpot down during a sync, a call and a write; calling continues; the 24-hour blocker fires and
      names its fix.

Build:

- [ ] R9B.6 `providers/hubspot.py`: Service Key auth, one pinned dated API version, paging and backoff.
- [ ] R9B.7 Settings row with a check on save; confirm and record the exact scopes and which property holds do-not-call.
- [ ] R9B.8 `leads/hubspot_sync.py`: list picker on Leads, Sync button, sync at Start calling.
- [ ] R9B.9 `aftercall/crm_log.py`: `log_call(CallRecord)`, outcome map and a retrying job that ends in Needs attention.
- [ ] R9B.10 Create the `phone_status` and `opendialer_call_id` properties once, at setup.
- [ ] R9B.11 Last-contacted guard.
- [ ] R9B.12 Layout audit covers the list picker, the spend limit and the Settings rows.

Waterfall and do-not-call on CRM leads (D19, D23), tests first:

- [ ] R9B.14 Port the 20 offline tests from `run_waterfall.py` as failing tests over recorded Treg responses, plus: a CRM
      number goes straight to Trestle; a repeat sync pays nothing; a changed CRM number is rechecked; the list limit stops the
      job and says so; no token or a Treg outage never blocks a lead that has a CRM number; a CRM number is never overwritten.
- [ ] R9B.15 Failing do-not-call tests over recorded RealValidito responses: each of `federal_dnc`, `state_dnc` and
      `tcpa_litigator` suppresses with its reason; `invalid` marks Bad; batches of at most 1,000; results expire after 30
      days; no key, error 603 or 604, or an outage leaves "DNC not checked" and blocks auto-dial with a named fix; a sync never
      clears a do-not-call suppression.
- [ ] R9B.16 `providers/treg.py` (Trestle, LeadOcean, AI Ark, Tomba, Wiza with polling) with an idempotency key, and
      `leads/waterfall.py` as a job after sync with a durable ledger.
- [ ] R9B.17 `providers/realvalidito.py` and `leads/dnc.py`; replace the DoNotCallDNC provider and its tests.
- [ ] R9B.18 Settings rows for the Treg token and RealValidito, each checked on save; spend limit and estimate on the list.
- [ ] R9B.19 Write `phone_status`, an empty phone field and do-not-call opt-outs back to HubSpot.
- [ ] R9B.20 After your yes and limit: run the waterfall and do-not-call check on the HubSpot test list, then on 20 real
      contacts; record cost, confirmed share and do-not-call hits in section 9A.
- [ ] R9B.13 Live L14 to L19, using only the owner's test contact and 203-821-1101. L20 only if Nooks is in use.

Done when: L14 to L19 pass, a forced HubSpot outage does not stop a test call, the waterfall and do-not-call tests pass, and
R9B.20 is recorded. **Owner sitting 4.**

### Phase 10: helper, summary, remaining pages and release

Goal: finish the remaining surface and ship.

Live helper and after-call:

- [ ] R10.1 Live helper: streamed tips and private help only; remove interpretation layers that nothing else uses.
- [ ] R10.2 After-call: one request over the whole transcript returns summary, suggested outcome and next step.
- [ ] R10.3 Fix the three verified defects by design: no batch-wide rejection, no 32-segment cutoff, no "partial" caused by an
      unassessed item.
- [ ] R10.4 V4.1 Flash reviews commitments that would create tasks; a failed item fails alone, with Retry.
- [ ] R10.5 Transcript retention of 30 days; remove the raw-speech store and expiry code; add a delete control.

Pages:

- [ ] R10.6 Leads and Lists become one page.
- [ ] R10.7 Numbers: list, buy, add, ready status, daily cap.
- [ ] R10.8 Script: clear "unsaved draft" state versus saved version; memory view.
- [ ] R10.9 Review: calls, timeline, follow-ups as compact rows, learning card.
- [ ] R10.10 Layout audit passes on every page.

Docs and release:

- [ ] R10.11 Rewrite README, GETTING_STARTED and ARCHITECTURE; move finished plans to `docs/history/`; simplify `AGENTS.md`.
- [ ] R10.12 Full live matrix L1 to L19, plus L20 if Nooks is in use.
- [ ] R10.13 Signed 2.0 through the gated release, on the owner's OK; verify installed data, version, signatures and rollback.
- [ ] R10.14 Confirm CI matches the "after the rebuild" table in section 12A and tag `v2.0.0`.

Done when: every live check passes and the owner signs off. **Owner sitting 5.**

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

## 15. What stays out of scope

- Multiple users, teams or hosted serving.
- Windows.
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
