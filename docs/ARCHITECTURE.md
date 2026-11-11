# How OpenDialer works

November 2026, version 2.1.0. The plan that produced this design, with its decisions and history, is
[REBUILD_PLAN.md](REBUILD_PLAN.md). Older design documents are in [history/](history/).

## One computer, one rep, one file

- **Desktop shell** (`apps/desktop/src-tauri`, Rust and Tauri): starts the backend as a private sidecar on localhost,
  keeps the computer awake while the rep is calling, and installs signed updates. Mac and Windows build from the same
  code.
- **Screens** (`apps/desktop/src`, React): the Dialer, Leads, Review, Numbers, Script and Settings.
- **Backend** (`services/backend/app`, Python and FastAPI): the rep's calling decisions and data. One SQLite file per rep, in the
  app's data folder. Saved keys are encrypted with a private key file beside it.
- **Relay** (`supabase/`): Telnyx sends call events to a small Supabase function; the backend fetches them over HTTP every
  20 seconds, sooner when Realtime wakes it, and acknowledges each one after it is saved. Calling never depends on
  Realtime.

In 2.1, separately provisioned computers can use one shared pool and one HubSpot portal. The relay supplies each
computer's allowed numbers and segment. It stores sticky number bindings, outbound reservations and callback routing
metadata. The desktop keeps lead names, scripts, notes and transcripts. See [relay provisioning](../supabase/README.md#shared-number-pool-21).

## The calling core

Everything that decides whether the next call happens is small and checked.

| Piece | File | What it does |
| --- | --- | --- |
| The one gate | `calling/blockers.py` | `blockers(facts)` lists what stops calling now, most basic first. The Dialer, the queue and Settings read only this. |
| Ways out | `calling/blockers.py` `EXITS` | Every blocker declares how it ends: by itself within N seconds, or by a named control. |
| Watchdog | `calling/watchdog.py` | A self-clearing blocker that outlives its promise is repaired (the relay loop restarts), then shown as stuck with Reconnect. |
| Readiness | `/v1/calling/readiness` | While paused, the Dialer shows how many leads can be called now, or the plain reason none can. |
| Screen | `calling/screen.ts` | The Dialer's phase is a pure function of one state; `keys()` says what each key does in it. |
| Loop | `calling/useCallLoop.ts` | Start, dial, voicemail skip, hang-up, save and the next call. Every key decides the screen at once. |
| Call states | `calling/state.py` | `DIALING` to `RINGING`, `HUMAN` or a mailbox, `ENDED` (waiting for an outcome) and `FINALIZED` (saved). |
| One decision | `calling/decide.py` | `decide(situation)` says whether the next dial happens, checked over every combination of its inputs. |
| Dial safety | `calling/dial.py` | A durable dial record and carrier command ID: a lead is never dialed twice, and a call can always be ended. |
| Callbacks | `telephony/callbacks.py` | While the app is open its call client stays registered and says so every 30 seconds; a callback from anyone rings it (D29). Otherwise, or unanswered, the greeting plays and a follow-up is kept. |
| Rep's audio | `calling/audio_leg.py` | Each call joins an audio leg this run of the app opened and the carrier reports live; any other is ended and a fresh one dialed. The Dialer dials only once its own audio client holds the leg, so the rep is heard and the transcript has sound. |

The queue (`scheduling/queue.py`) offers callbacks due first, then leads never called, then the one waiting longest
since its last try (D31), inside each lead's calling hours
(8 AM to 5 PM local), with at least a day between tries and per-company spacing. When no one can be called, it says
why in plain words. A test call to the rep's own phone skips hours and attempt limits; an opt-out still applies.

### How the rules are checked

| Check | File | What it proves |
| --- | --- | --- |
| Every input combination of the Dialer screen | `apps/desktop/tests/screen-model.test.ts` | Always a way forward; Pause reachable; an ended call is saved before anything else. |
| Every combination of blocker facts | `tests/test_blockers.py` | Anything wrong names a blocker; each has a way out. |
| Each blocker in and out | `tests/acceptance/test_blocker_exits.py` | All 21 blockers clear through their real code path. A new blocker without a test fails CI. |
| Fault soak | `tests/test_fault_soak.py` | Random runs with clock jumps, duplicate and late events and opt-outs: no server error, never two open calls, no dial after an opt-out, Pause honored, and recovery always dials or says why. |
| The rep's own key | `apps/desktop/tests/screen-model.test.ts`, `simple-calling.spec.ts` | Hang up, Skip, Stay and the outcome keys show at once over any re-read (`withIntent`); one save at a time; a Pause pressed during any start step wins. |
| Layout | `apps/desktop/tests/layout-audit.spec.ts` | On every page, at both window sizes, the main control stays uncovered and still. |

Every screen that re-reads on events shows only the newest answer (`state/newest.ts`), so a slow older read never
undoes a newer one. Every handled failure logs a line; a silent `except Exception` fails the build.

CI runs every browser test once, in WebKit (the Mac app) and Chromium (Windows), so a flaky test fails instead of
hiding a defect.

## Leads and numbers

- **Intake** (`leads/intake.py`): one column map for CSV and HubSpot. Only mapped columns reach a model.
- **HubSpot** (`leads/hubspot_sync.py`, `crm.py`): a Service Key; a list picks who is called. Each sync adds, updates
  and removes leads; opt-outs flow both ways; each call is logged as a HubSpot call record with the summary.
- **Number checks** (`leads/waterfall.py`): for HubSpot leads only, through Treg, within a spend limit per list. CSV
  numbers are never checked. A number that may belong to someone else is held and shown in Review.
- **Do-not-call** (`leads/dnc.py`): RealValidito for HubSpot leads, refreshed every 30 days.
- **Numbers** (`numbers/`): the rep's own Telnyx numbers, one row each with Pause and Resume.

## Research, scripts and after the call

- **Research and scripts** (`prep/`, `preparation/`): the ChatGPT login is the main route; DeepSeek V4.1 Flash on
  OpenRouter's no-retention route takes over when it is unavailable. Each lead gets a trigger ("why now"), checked
  against the page it came from.
- **Live helper** (`helper/live.py`, wired in `live_help.py`): while the prospect talks, one short line the rep could
  say or ask next, with the part of the script that fits the moment (the script goes to the model as numbered parts and
  each tip names one); when the rep asks (F1 or a typed question), a private answer. One streamed request per turn on
  OpenRouter, served only by Cerebras with no retention. A tip lives in memory for 30 seconds (an answer for 5 minutes)
  and is never saved. Without an OpenRouter key the helper says so and the script carries the call.
- **After the call** (`aftercall/summary.py`): one request over the whole transcript returns a summary, a suggested
  outcome and a next step; each field is checked on its own, and a failed summary has Retry. It is shown in the
  wrap-up and in Review, read by the next call's script and brief, and added to HubSpot. The rep confirms the
  suggested outcome; only carrier facts (no answer, busy, a mailbox) save themselves.
- **Transcripts** are kept on this computer for 30 days, then deleted; the rep can delete one sooner. There are no
  recordings.

## Learning

StatML is archived (D31, tag `statml-archive`). The one learning view kept is stage drop-off: each after-call summary
names the script part the call ended in, and Review shows where answered calls end, by outcome
(`aftercall/dropoff.py`). Number health compares each number's pickups with the pickup rate across all numbers.

## Releases

`main` must pass the fast check on commit and the full check (backend, frontend, Rust, WebKit browser suite) on push.
A signed build only comes from a commit whose CI passed; each release carries the Mac app, the Windows installers and
an update manifest. Installed data is kept across updates.

## What is still old code

Some legacy modules (`service.py`, `administration.py`, `rep_workflows.py`, `intelligence.py`, `managed_webhooks.py`)
are being replaced piece by piece (decision D24). New packages never import them; `tests/test_imports.py` enforces
that. The plan lists what remains.
