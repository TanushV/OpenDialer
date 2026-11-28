# How OpenDialer works

November 2026, version 2.1.0. The plan that produced this design, with its decisions and history, is
[REBUILD_PLAN.md](REBUILD_PLAN.md). Older design documents are in [history/](history/).

## One computer, one rep, one file

- **Desktop shell** (`apps/desktop/src-tauri`, Rust and Tauri): starts the backend as a private sidecar on localhost,
  keeps the computer awake while the rep is calling on Mac and Windows, and installs signed updates. Mac, Windows and
  Linux build from the same code. The Linux package uses WebKitGTK on Ubuntu 24.04 LTS x86-64.
- **Screens** (`apps/desktop/src`, React): the Dialer, Leads, Review, Numbers, Script and Settings.
- **Backend** (`services/backend/app`, Python and FastAPI): the rep's calling decisions and data. One SQLite file per rep, in the
  app's data folder. Saved keys are encrypted with a private key file beside it.
- **Relay** (`supabase/`): Telnyx sends call events to a small Supabase function; the backend fetches them over HTTP every
  20 seconds, sooner when Realtime wakes it, and acknowledges each one after it is saved. Calling never depends on
  Realtime.

In 2.1, separately provisioned computers can use one shared number pool, one Telnyx Call Control application and
one HubSpot portal. Each computer gets a distinct SIP credential, relay workspace and HubSpot segment. The relay
keeps each prospect's number, matches signed events to the workspace that placed the call, and gives a return call
to that workspace. When the hosted fallback is enabled, it can play the greeting for an offline rep. The desktop
keeps lead names, scripts, notes and transcripts. See [relay provisioning](../supabase/README.md#shared-number-pool-21).

## The calling core

Everything that decides whether the next call happens is small and checked.

| Piece | File | What it does |
| --- | --- | --- |
| The one gate | `calling/blockers.py` | `blockers(facts)` lists what stops calling now, most basic first. The Dialer, the queue and Settings read only this. |
| Ways out | `calling/blockers.py` `EXITS` | Every blocker declares how it ends: by itself within N seconds, or by a named control. |
| Watchdog | `calling/watchdog.py` | A self-clearing blocker that outlives its promise is repaired (the relay loop restarts), then shown as stuck with Reconnect. |
| Readiness | `/v1/calling/readiness` | While paused, the Dialer shows how many leads can be called now, or the plain reason none can. |
| Screen | `calling/screen.ts` | The Dialer's phase is a pure function of one state; `keys()` says what each key does in it. |
| Rep action | `calling/useCallLoop.ts` | Each trusted click or fresh keyboard action grants one request to start an outbound call. The Dialer waits for another action after the result. |
| Call states | `calling/state.py` | `DIALING` to `RINGING`, `HUMAN` or a mailbox, `ENDED` (waiting for an outcome) and `FINALIZED` (saved). |
| One decision | `calling/decide.py` | `decide(situation)` says whether the next dial happens, checked over every combination of its inputs. |
| Dial safety | `calling/dial.py` | Only a live start request can submit the fresh dial it created. Recovery closes expired unsent attempts and uses the original carrier reference to reconcile or stop submitted attempts. |
| Callbacks | `telephony/callbacks.py` | While the app is open its call client stays registered and says so every 30 seconds; a callback from anyone rings it (D29). Otherwise, or unanswered, the greeting plays and a follow-up is kept. |
| Rep's audio | `calling/audio_leg.py`, `telephony/client.ts` | The carrier and desktop verify the rep's leg before dialing. The desktop checks that the browser can play its audio and routes ringback, the end tone and the call to the selected output. |

The queue (`scheduling/queue.py`) offers callbacks due first, then leads never called, then the one waiting longest
since its last try (D31), inside each lead's calling hours
(8 AM to 5 PM local), with at least a day between tries and per-company spacing. When no one can be called, it says
why in plain words. A test call to the rep's own phone skips hours, attempt limits and outreach holds, and gets the saved base script, speech and the live helper; an opt-out still applies.

### How the rules are checked

| Check | File | What it proves |
| --- | --- | --- |
| Every input combination of the Dialer screen | `apps/desktop/tests/screen-model.test.ts` | Always a way forward; Pause reachable; an ended call is saved before anything else. |
| Every combination of blocker facts | `tests/test_blockers.py` | Anything wrong names a blocker; each has a way out. |
| Each blocker in and out | `tests/acceptance/test_blocker_exits.py` | All 21 blockers clear through their real code path. A new blocker without a test fails CI. |
| Fault soak | `tests/test_fault_soak.py` | Random runs with clock jumps, duplicate and late events and opt-outs: no server error, never two open calls, no dial after an opt-out, Pause honored, and recovery always dials or says why. |
| The rep's own key | `apps/desktop/tests/screen-model.test.ts`, `simple-calling.spec.ts` | Hang up and the outcome keys show at once over any re-read. Playback failures are visible, and only the rep can dial the next lead or end a mailbox; there is no voicemail detection, and an End as voicemail inside 7 seconds of answer holds the carrier stop to 7 seconds (`calling/hold.py`). |
| Layout | `apps/desktop/tests/layout-audit.spec.ts` | On every page, at both window sizes, the main control stays uncovered and still. |

Every screen that re-reads on events shows only the newest answer (`state/newest.ts`), so a slow older read never
undoes a newer one. Every handled failure logs a line; a silent `except Exception` fails the build.

CI runs every browser test once, in WebKit (the Mac app) and Chromium (Windows), so a flaky test fails instead of
hiding a defect.

## Leads and numbers

- **Intake** (`leads/intake.py`): one column map for CSV and HubSpot. Research reads all CSV columns with bounded
  cell and row sizes. HubSpot supplies identity and mapped columns.
- **HubSpot** (`leads/hubspot_sync.py`, `crm.py`): a Service Key and an optional owner per desktop attribute call
  activity. A shared profile syncs its assigned segment and can also call local lists. The CRM worker links or creates
  a CSV prospect after the first connected call and logs the outcome. Test calls stay local.
- **Test phones** (`calling/test_calls.py`, `test_list.py`): Settings stores approved destinations separately from
  outreach. The Dialer reserves them directly and uses the same carrier command, playback and recovery code.
  Test outcomes live with the call. A test never starts CRM sync, research, script generation or outreach cadence.
- **Number checks** (`leads/waterfall.py`): for HubSpot leads only, through Treg, within a spend limit per list. CSV
  numbers are never checked. A number that may belong to someone else is held and shown in Review.
- **Do-not-call** (`leads/dnc.py`): RealValidito for HubSpot leads, refreshed every 30 days.
- **Numbers** (`numbers/`): the rep's own Telnyx numbers, one row each with Pause and Resume.

## Research, scripts and after the call

- **Research and scripts** (`prep/`, `preparation/`): the ChatGPT login is the main route; DeepSeek V4.1 Flash on
  OpenRouter's no-retention route takes over when it is unavailable. Each lead gets a trigger ("why now"), checked
  against the page it came from.
- **Live helper** (`helper/live.py`, wired in `live_help.py`) sends the call's prepared script in its authored order and
  asks for one or two spoken sentences. Replies can use, adapt or go beyond the script and calculate estimates from
  stated inputs. The direct audio path uses the latest transcript after 240 ms of detected quiet. It gives obviously
  unfinished phrases 540 ms. Telnyx can finalize a segment during a pause, so the local audio determines when to ask.
  Transcript-only inputs still coalesce across a one-second wait. A local reply can appear after 220 ms while the same
  model request continues. Exact script quotes link back to the matching line. The rep can also ask via F1 or a typed
  question. One request per turn runs on OpenRouter through Cerebras with no retention. Tips stay in memory for 30
  seconds and private replies for 5 minutes. Phone screening uses the prepared Screening line directly.
- **After the call**: no summary is written. The carrier saves facts such as no answer or busy; the rep saves every
  other outcome, including whether a voicemail message was left. The next call's script and brief read the outcome
  and the rep's note.
- **Call records** (`call_records.py`, `calling/audio_tracks.py`; call insights plan P1 to P4): live speech comes only
  from the app's own two audio tracks (the carrier never transcribes). The rep's side (audio and transcript) is kept
  on every answered call; the prospect's side only when both their supplied state and their area code's state allow
  one-party consent, or from the moment the rep presses R after saying the call is recorded. Live transcription for
  the helper runs on every call unless Settings holds it in stricter states. Text a call may not keep is deleted when
  it ends; a 3-hour backstop sweeps leftovers and SQLite overwrites deleted text. Audio is recorded on this computer
  from the same PCM the transcriber gets, encoded as stereo Ogg Opus (rep left) and encrypted with the workspace key.
  Recordings are kept until deleted, with optional auto-delete; an opt-out deletes that phone's recordings. Rep
  segments that repeat the prospect at the same moment are speaker bleed and never count as the rep's speech.
- **Call numbers and insights** (`call_metrics.py`, `insights.py`, `call_export.py`): each finalized call gets a fixed
  list of numbers (talk share, monologues, response gap, interruptions, time to pitch, the rep's pitch range and
  loudness) and never a voiceprint. Review compares results by script variant, path, local time band, list, trigger
  or a number, with 95% ranges, and exports calls with their audio and kept transcripts as one ZIP.
- **Calling hours and opt-outs**: outreach dials Monday to Friday, 9:00 to 17:00 in the prospect's zone, never on US
  federal holidays; requested callbacks are exempt (`compliance/policy.py`). Numbers outside the 50 states and DC are
  held. Inside the window the queue prefers each lead's local prime time (`scheduling/prime_time.py`), and on shared
  calling a prospect another rep called in the last day waits a day. Every rep pulls HubSpot opt-outs at
  Start calling, after imports and every 15 minutes (`crm.pull_opt_outs`).

## Learning

StatML is archived (D31, tag `statml-archive`). The one learning view kept is stage drop-off: the last script part the
rep's speech matched during the call, kept as word-free metadata, and Review shows where answered calls end, by outcome
(`aftercall/dropoff.py`). Number health compares each number's pickups with the pickup rate across all numbers.

## Releases

`main` must pass the fast check on commit and the full check (backend, frontend, Rust, WebKit browser suite) on push.
A signed build only comes from a commit whose CI passed. Each release carries the Mac app, Windows installers, the Linux
Debian package and an update manifest. Installed data is kept across updates.

## What is still old code

Some legacy modules (`service.py`, `administration.py`, `rep_workflows.py`, `intelligence.py`, `managed_webhooks.py`)
are being replaced piece by piece (decision D24). New packages never import them; `tests/test_imports.py` enforces
that. The plan lists what remains.
