# Calling, test calls and portable scripts: implementation and release plan

Owner scope approved November 22, 2026. **Planning only: do not start execution until the owner says to begin.**
The owner authorized implementation, testing, randomized stress testing, commits and pushes to GitHub, a new published
version, and installation through the signed updater on this Mac, but explicitly paused execution while requesting
this written plan. No separate approval for those already-authorized actions is needed once execution is started.

Read this file and [REBUILD_PLAN.md](REBUILD_PLAN.md), including both checkpoints, after every continuation or compaction.
This is the current work package under the rebuild plan. The November 22 owner decisions below replace older wording
that excludes scripts/speech from test calls or retains answering-machine detection. Keep the rebuild plan's working
rules: one worker/thread, direct main, no worktrees or subagents, regression tests before fixes, full local checks before
every push, and exact-commit green CI. Preserve unrelated dirty work and the installed workspace.

**November 22 update.** The owner approved the recommendations now written into sections 4 to 9, including advisory
voicemail guidance (6.12) and the legal compliance audit (section 9). The owner answered D1 to D5 the same day
(section 9.4), then D6 and D7 (time zones and transcripts). Execution is still on hold until the owner says to begin.

## 1. Required result

- Every test call uses the normal audio, ringback, call controls, base script, transcription and live helper.
  Test mode excludes outreach eligibility and side effects. It must remain useful for repeatable realistic practice.
- Generated prospect scripts contain one coherent authored track selected using persona and verified signals.
  Shared objections, questions, scenarios, cues and other shared material retain their authored wording and order.
  The model selects only declared alternatives and fills only designated personalization blocks.
- The external generator produces completed per-person scripts locally using the app's shared validation/rendering
  machinery. Reps import its CSV and can use the finished scripts without running generation on their computers.
- Voicemail detection is removed. The rep hears the call and chooses when to end it and which outcome to save.
  The app tells the rep, per attempt, whether to leave a voicemail and shows the wording; it never enforces it.
  An early End as voicemail holds the carrier line to 7 seconds after answer to stay within carrier short-call rules.
- Outreach calls meet the compliance requirements in section 9: a time-zone-aware weekday calling window, seller-wide
  opt-outs through HubSpot, caller identification and a Settings switch for live transcription. The owner screens
  supplied lists against the National Do Not Call Registry before import; the app does not (owner decision).
- Transcripts are never kept. They exist only while a call is live, feed only the live helper and script following,
  and are deleted when the call ends (section 9.3 C5).
- Every outbound attempt requires a new deliberate rep action. Ending, saving, timers, restarts and recovery cannot
  start the next attempt. The helper cannot dial, hang up, disposition, suppress or advance calls.
- The test suite is audited for correctness and exercised against fresh, long randomized sequences. Passing means
  independently stated invariants held for the executed cases, not that a language model wrote a convincing suite.
- Deliver tested source on GitHub, a new public signed release with working update metadata and supported platform
  artifacts, then update and verify this Mac without losing credentials, lists, scripts or call history.

## 2. Baseline and defects to reproduce first

The investigation inspected main at `5466b36`, version 2.4.1. Refresh checkout, installed/public versions and exact-head
CI when execution starts. These are source/isolated-test findings, not proof of the other computer's exact installation.

Refreshed November 22 (read-only): `main` at `5466b36` has green Verify and Signed release runs. The latest public
release is 2.4.1 and a leftover 2.4.0 draft release exists. This Mac runs 2.4.0 with the Agite workspace (306 leads,
293 scripts ready), which must survive the update. Detection already never sends a hangup (only legacy
`machine-hangup` handling remains), but `calling/dial.py:75` still requests premium detection for outreach and test
lists, and no minimum call duration exists anywhere.

| Area | Observed behavior | Required regression |
|---|---|---|
| Test script | `calling/dial.py` returns no script for a test contact; the frontend drops its prospect packet. | A saved base script is visible before and during both single-phone and test-list calls. |
| Test helper | `privacy.py` rejects test transcripts; `live_help.py` reports the test call ended; the frontend hides its helper. | Actual test-call transcript ingestion produces a helper response while the call is active. |
| Test speech startup | The answer handler starts transcription only when it has an outreach lead. | Direct-track setup and the supported carrier transcription route work with a test contact. |
| Test eligibility | The test route still reads shared endpoint holds and suppressions. | Approved practice destinations do not inherit outreach holds or acquire permanent holds from practice speech/outcomes. |
| Detection | Single-phone tests disable detection; test lists and outreach still request premium detection. | Every newly dialed call disables detection and shows Connected without waiting for classification. |
| Callback verification copy | Settings uses an unformatted catalog message containing `{phone}` and “One test call left.” | Show the real pending callback-verification step, with a usable destination/instruction and a tested exit. |
| Callback context | The callback panel shows a brief, but does not render the prepared script; test callbacks skip the prospect fetch. | Matched callbacks show the appropriate script without looking up a test identity as a real lead. |
| Generator defaults | `saved_playbook()` returns master, seller, rep; its caller interprets master, rep, seller. | Default exported wording uses the right business and rep identities. |
| Portable identity | Generation fills the source rep name into the exported script. | Rep B imports the generated CSV and sees Rep B, with the same prospect wording. |
| Batch recovery | The external generator writes its CSV after all jobs finish. | Completed validated rows survive interruption and resume without redundant generation. |
| Provenance | Finished CSV import retains text but loses the source path/master identity. | Imported scripts and call-statistics exports retain validated source identity when supplied. |

The prior investigation passed 22 focused script/import tests. A disposable round trip with a simulated model reply
passed the real generation validators, exported/imported a CSV, and placed a simulated call with its script and no
generation jobs. It also reproduced both identity bugs. These checks are a starting point, not release acceptance.

The saved master inspected during the investigation had three paths, four AI-writing blocks, eleven objections,
nine “If the context is wrong” scenarios and six “Questions they ask” responses. The last three groups had no AI blocks
or path tags. Refresh this inventory before using the owner's master as a regression fixture; do not hardcode its
headings, counts or business content into the product.

## 3. Test calls: shared call experience, isolated outreach effects

1. Keep independent approved test contacts and the existing durable call/command/recovery infrastructure. Do not create
   a second dialer or put test contacts back in ordinary lead lists to obtain a script.
2. Build the minimum shared call context needed by script display, speech, auto-follow and helper requests. It resolves
   either an outreach lead or a test contact explicitly; never query the leads table with a test-contact ID.
3. Snapshot the saved **base script** when the test starts. Show it in authored order using the normal reader. Supply
   its text and the current conversation to the same helper. No test research, generation, prepared status, selected
   persona/path or strict section schema is required. Do not build a rehearsal-script chooser for this work package.
4. Use the test contact's display name where useful and the current rep/business identity. Do not invent research,
   borrow a matching prospect's prior conversations or mutate that prospect. A master edit during a call must not
   replace the snapshot underneath it; the next test takes the new master.
5. Enable normal speech startup, ingestion, turn detection, helper requests and script following for tests. Remove
   all test-only exclusions together, including duplicate/revised transcript handling and direct-track setup.
6. The helper can quote, fill or adapt script wording, answer off-script questions and use conversation context. It
   must not require a structured tool selection to return useful text. Keep one model request per substantive turn
   where possible; interim/final revisions must not create duplicate requests or resurrect tips from a previous call.
7. Practice statements such as “not interested,” “wrong number” and “stop calling” are test dialogue. Do not turn
   them into a persistent outreach suppression, CRM write, ended helper session or automatic hangup. Do not build a
   separate practice opt-out control (owner decision, November 22); a new deliberate test stays callable. Preserve real outreach opt-outs and their enforcement unchanged.
8. Bypass outreach hours/time zones, cadence, attempt exhaustion, company spacing, CRM history/segment eligibility,
   DNC/research/preparation status, and outreach endpoint holds for explicitly staged test destinations. Do not erase
   existing real endpoint holds to achieve this. Legacy test-created holds may remain on outreach records without
   gating isolated practice calls. Do not offer arbitrary prospect calls through the test route.
9. Keep actual transport requirements: valid approved destination, working credentials/route, caller-ID availability,
   audio access, one active attempt and one fresh user action. Carrier/account capacity and network failures are real
   failures, not something an app flag can bypass. They must name the failure and its actionable exit.
10. Keep test receipts local and clearly marked (test transcripts follow the no-keep rule in section 9.3 C5); exclude them from production outreach statistics and CRM
    writes, including jobs left over from older versions. Other call records keep their retention/delete behavior. Neither
    test-contact imports nor test outcomes should schedule paid lead research, screening or enrichment. Ordinary
    CSV imports arrive already screened by the owner (section 9.4 D2); the app does not screen them.
11. A missing base script, missing helper credential or failed speech stream must be explicitly visible with a remedy;
    do not silently show an empty card or claim an active call has ended. A helper/script failure must not end a live
    call. Expose degraded assistance separately from carrier-call readiness instead of adding an outreach gate.

## 4. One generated track with unchanged shared content

1. Reuse the existing declared paths and option annotations. Select one path from research/persona/signal evidence,
   using the authored default when evidence is inconclusive. Selection cannot introduce unsupported signal claims.
2. Select only alternatives that the author designated as choices. No broad summarization, rearrangement or rewriting
   of the master. Fill the selected `{{ ... }}` blocks with supported person/company context; ordinary placeholders
   are simple substitutions. Preserve the literal surrounding script and authored order.
3. Include every shared objection, scenario, question/answer, cue and reference in its original position. Those are
   the rep's alternatives during a conversation, not competing persona tracks for generation to delete.
4. Audit the existing heading-based pick limits, especially Discovery and Offers. They must not silently trim shared
   content or choose extra persona tracks. Preserve backward compatibility for existing masters; if explicit choice
   or shared annotations are needed, make them generic and document/preview them. Never infer the owner's intent
   from a hardcoded list of business-specific section names.
   **Decided (November 22).** Today `preparation/templates.py` `PICKS` keeps 1 to 3 Discovery bullets and 1 to 2
   Offers cards per lead; objections are always shared; unrecognized headings are kept whole as fixed sections. New
   rule: when the master defines `# Paths`, items tagged `[path: ...]` are alternatives for their path, and untagged
   items in list and card sections are shared, kept whole and in order; `PICKS` never trims them. Choice sections
   (Opening, Gatekeeper, Pitch, Close) still pick one option for the selected path. A master without paths keeps
   today's picks, and the preview states them plainly (for example "The AI picks up to 3 of your 6 Discovery
   questions per lead"). Read the owner's saved master read-only before implementing and confirm the rule fits it.
   **Owner note (November 22).** Paths depend on the lead's circumstances and inputs (for example, a sales leader
   with a signal takes a specific text chunk). Choose the path from persona and verified signal evidence first, then
   generate only for that path: the model fills `{{ ... }}` blocks only inside the chosen path's options and shared
   content it is allowed to personalize, never inside another path's options, which are dropped before generation.
   The selection reason (persona, signal) is stored with the script for preview, calls and statistics.
5. Validate selected-path membership, exact shared-content preservation and source ordering before accepting a script.
   For shared content designated unchanged, the model cannot author blocks inside it. If an ambiguous master violates
   that contract, show a line-specific format error and a minimal correction rather than silently dropping text.
6. Do not auto-infer branches from arbitrary prose in old masters without annotations. Explain missing/ambiguous path
   markup in the preview, while preserving ordinary base-script use. Apply format changes only as needed and retain
   the saved original. Test custom headings, renamed sections and reordered structures.
7. Leads preview, the call snapshot, auto-follow, the helper and statistics must all use the same completed version.
   Preserve per-list generation controls, current-master pause/resume and rejection of late results from a replaced
   master. Existing imported finished scripts remain ready through unrelated master edits.

## 5. External generator and CSV handoff

The runner remains at `/Users/tanushv/Documents/OpenDialer Script Generator`, outside the app and repository. Keep its
code, launcher, local tests and handoff instructions there. Do not bundle it in installers or copy it into the repo.
App-side generic parser/importer improvements and their fixture-based tests belong in the repo.

1. Fix the saved rep/seller tuple ordering. Share the app's prompt contract, tool schemas, path validation, factual
   validation and renderer where practical; avoid maintaining a subtly different definition of a valid script.
   The external runner may orchestrate local Codex jobs, but production app code must not depend on its folder.
   Decided: the runner imports the validator, renderer and tool schemas from a checkout of this repo at the release
   tag the receiving app runs (a path dependency, never a copied file). Each completed CSV carries `script_format`;
   the importer accepts older formats and refuses a newer one with "Update OpenDialer to import this file".
   Checkpoint by appending each validated row to a partial file, flushed per row, with a manifest of the input and
   master digests; rename to the final CSV only when every row is complete or individually marked failed.
2. Export completed per-prospect wording while preserving portable `{rep}` and `{seller}` tokens for the receiving
   workspace to fill. Do not export credentials or require the receiver to import the author's model login just to
   use finished scripts. The live helper still needs its own configured inference access.
3. Add bounded, documented optional provenance fields for the source-master digest/version, selected path and
   generator format version. Preserve validated research/brief context needed for the same calling experience,
   distinguishing verified evidence from row-only observations. Do not pass metadata off as new verified facts.
4. Keep older CSVs with only a `script` column working. Detect and explain malformed metadata or unfinished scripts
   per row; imported metadata must not be interpreted as another script column or silently change the selected text.
5. Checkpoint each validated completed row atomically. Resume only compatible completed work using input/master
   fingerprints; explicitly report a changed master or lead input. Never silently mix incompatible runs or overwrite
   the user's original input. Preserve finished compatible rows and make failed rows individually retryable.
6. Verify the full route with separate author and receiver workspaces: input CSV -> generation/validation -> completed
   CSV -> ordinary import -> Ready -> same script in the Dialer -> helper response. Receiving-side generation jobs
   remain zero. Ordinary outreach safeguards still apply to ordinary imported prospects.
7. Cover quoted multiline Markdown, commas/quotes, Unicode/BOM, sparse and wide rows, mixed ready/blank scripts,
   conflicting script columns, large batched imports, interruptions, duplicate/reimported contacts, master changes,
   and another rep/business identity. Keep the entire source row available to research/writing under bounded access.
8. Update the outside-folder README with one copy/paste generation command, output location, resume/retry behavior,
   and brainless receiver instructions: open Leads, open the completed CSV, review/import, and use the ready scripts.
   Verify one bounded real local generation sample separately from stubbed-model contract tests when execution begins.

## 6. Manual call control, voicemail and callbacks

1. Disable answering-machine detection on every new prospect/test dial. Remove detection-dependent waiting text,
   timers, reports and decisions where no longer useful. Old delayed detection receipts must be harmless and must
   never enqueue an automatic hangup. Preserve readable historical records without replaying obsolete commands.
2. On carrier answer, show Connected and continue audio/speech/helper immediately. Do not label it Human without
   evidence. Keep phone-screening assistance advisory; neither it nor a model suggestion controls the call lifecycle.
3. Ringback uses the selected output and hands off to remote audio. Voicemail greeting and beep remain audible.
   No app-driven voicemail timeout/hangup. Carrier disconnects, transport loss and explicit rep controls still have
   real effects; do not promise an infinite call or conceal a carrier-imposed duration limit.
4. Keep a clearly labeled End as voicemail control available after answer, without requiring detection. Its explicit
   shortcut is Space; Escape remains generic Hang up. While dialing/ringing, Space ends that attempt. In a saved/ready
   state, a new Space can start one next attempt. Ignore held/repeated keys, focused text inputs and synthetic events.
   One press must never both end the current call and dial the next, even when its key-up arrives after a state change.
5. Drive voicemail/human reporting and CRM mapping from explicit outcomes and carrier facts after detection is removed.
   A mailbox answering must not create a false human conversation or trigger CSV contact creation as one. Preserve
   no-answer/busy/failure receipts and don't invent an outcome when the carrier did not prove it.
6. Matched callbacks show the prior call's script/context where available, with a safe base-script fallback. Test
   callbacks use test context. Reuse the normal reader instead of a divergent rendering implementation. Check callback
   speech/helper integration as well; display any unavailable capability rather than silently implying it is active.
7. Fix callback ring ownership/state races: ring while the app can answer and the configured incoming ring window is
   open; stop on answer, caller hangup, explicit decline or the actual greeting transition. No hidden frontend timer
   should stop it early. Answering must not require outgoing readiness or outbound capacity.
8. Replace “One test call left”/literal `{phone}` with callback verification instructions. It is a one-time workspace
   readiness proof, not a test-call allowance. Ensure an answered verification callback clears it and the proof
   persists through ordinary restarts and same-profile reimport. Give a changed profile an honest new state.
9. Keep existing numbers, number assignments, sticky caller IDs and rep setup identities. No new relay project,
   portal sign-ins or number moves are part of this work. If testing reveals a necessary hosted change, establish
   the precise defect and smallest change before extending scope; do not silently reconfigure production. No hosted
   change is planned: opt-outs are shared through HubSpot (section 9.3 C3) and the callback fallback stays as it is
   unless the owner chooses otherwise in D6.

**Decided November 22 (owner approved).**

10. **Short-call hold.** Telnyx counts connected calls of 6 seconds or less as short calls; above 15% of a UTC day's
    connected calls it emails, may charge, and says it does not support such traffic. When the rep presses End as
    voicemail within 7 seconds of answer, cut the rep's audio and microphone at once, stop transcription and show the
    outcome step, but send the carrier hangup at answer + 7 seconds as a durable command for that leg only. Escape
    (Hang up) and any end before answer stay immediate, so a person never hears held silence after an ordinary hang-up.
    The next dial waits for the held leg to close. A restart sends an overdue held hangup at once, never early.
11. **Space re-arm.** Keep the existing trusted, non-repeat, single-use key guards (`useCallLoop.ts`, `screen.ts`).
    After a call ends, Space cannot dial until the app has seen Space released in the new state. Test in WebKit (the
    Mac app's engine) and Chromium.
12. **When to leave a voicemail (advisory, never enforced).** Each attempt carries a voicemail cue computed when it is
    dialed from the list's rule. Default: leave one on the first and the final attempt, on a callback the prospect
    requested, and after an earlier conversation; otherwise skip. Lists choose first and final (default), final only,
    every attempt, or never; this replaces `voicemail_enabled`, and existing workspaces get the default. The call card
    shows the cue and its reason from dial onward ("Voicemail: leave one, final attempt" / "Voicemail: skip, message
    left on attempt 1"). When it says leave, the reader shows the prospect's `# Voicemail` wording after answer.
    Nothing is blocked or automated: End as voicemail, staying on the line and speaking work the same under either
    cue. Store the cue on the call. The rep records what happened with the two existing outcomes, `voicemail` (left a
    message; HubSpot Left voicemail) and `voicemail_skipped` (HubSpot No answer), both offered after End as voicemail
    with neither preselected. Never infer it from timing or speech.
13. **Voicemail wording.** `# Voicemail` must include `{rep}`, `{seller}` and a new `{callback_number}` placeholder
    (filled with the caller ID used on that call). A missing one is a line-specific format error with its fix. Older
    masters keep working as base scripts; the cue shows "Add voicemail wording to your script" until it is fixed.
14. **No detection, no voicemail drop, no artificial voice.** Every new dial sends detection disabled. Recorded or
    synthesized messages to called parties stay removed: the FCC treats prerecorded and AI-generated voices as
    artificial, and a prerecorded telemarketing message to a mobile needs prior express written consent.
15. **Screening help.** Carrier screening detection goes with detection. The helper recognizes screening prompts from
    the transcript ("state your name and why you're calling") and offers the `# Screening` wording. Advisory only.
16. **Detection removal surface.** Replace every read of `human_answered_at` or `machine_detected` as "a person was
    reached" with the explicit outcome set in `calling/detection.py` `PERSON`: `crm.py:680` (CSV contact creation),
    `call_statistics.py:162`, `intelligence.py:567/584`, `preparation/workflows.py:439`, `calling/dial.py:568`,
    `calling/timeline.py`, the stay-on-voicemail command in `telephony/workflows.py`, and frontend `useCallLoop.ts`,
    `screen.ts` and `callStates.ts`. Retire `detection.report`. SCREENING, VOICEMAIL_GREETING and VOICEMAIL_READY stay
    readable for history, but new calls never enter them and late detection events change nothing. The calling
    budget must report fewer states and timers.
17. **Rate readout.** Review shows today's (UTC) short-call share of connected calls and abandoned share of outbound
    calls from the app's own records. Telnyx charges $0.005 per abandoned call once they exceed 20% of outbound calls
    (hang-ups while ringing, and calls to disconnected numbers). Information only: no blocker, no gate.

## 7. Audit and strengthen the randomized stress suite

### What the suite must prove

Random sequences are generated at runtime, with fresh recorded seeds, variable lengths and independently varied
timings/faults. They are not a fixed set of surprising examples produced by GPT. Models can suggest adversarial cases;
reviewed executable assertions and observable effects decide pass/fail. No finite suite can prove every possible
scenario, but the following contract must hold throughout every run, with coverage and limitations reported.

| Invariant | Independent observable check |
|---|---|
| No automatic/duplicate dialing | Count actual outbound carrier submissions before carrier deduplication; each needs a unique fresh rep authorization. Queue work, timers, callbacks, recovery and saved outcomes grant none. |
| Stops target the intended call | Inspect target leg/command IDs across delayed replies, a previous call ending late and a later call starting. Unsent cancellations never later submit. |
| Hangup remains usable | Exercise server command and local audio stop paths; accepted stops settle when the required provider becomes available, otherwise show the precise unresolved state. |
| No voicemail auto-hangup | Detection/beep/quiet/long greetings cause no app hangup or next dial; an explicit rep action remains necessary. |
| Test isolation | Compare outreach records, endpoint holds, CRM effects and preparation jobs before/after practice, including a matching real phone number. They remain unchanged. |
| Realistic assistance | A call with a saved script displays it; accepted prospect turns exercise speech/helper and produce a reply or visible actionable failure. Blank UI or silent discarded turns fail. |
| One coherent script | Compare the call to its immutable authored/validated snapshot: selected track, shared responses, order, rep identity and provenance. |
| Correct callback | Verify intended workspace, recipient/script and ring lifecycle independently of outbound capacity and paused outreach. |
| Contained failure and recovery | When dependencies recover, reconcile safely or expose a reachable remedy; never hide the failure, repeat an uncertain external effect or silently start calling. |
| Durable receipts | Restarts keep call identity, confirmed effects, outcomes and trace correlation without treating simulated receipts as live evidence. |
| Short-call hold | After End as voicemail within 7 seconds of answer, the carrier hangup is sent no earlier than answer + 7 seconds and only for that leg; Escape and pre-answer ends are immediate; no next dial while the held leg is open. |
| No transcript after the call | After any call ends, including a crash and restart, no transcript or helper-tip text remains in the database, logs, receipts, HubSpot or test artifacts. |
| No artificial voice outbound | No speak, playback, gather or record command is ever sent on an outbound leg, including old queued commands and recovery. |
| Compliance gates | Outreach dials (not owner test lines, not requested callbacks) never happen outside Monday to Friday 9:00 to 17:00 recipient local time or on federal holidays, or to a number opted out in HubSpot or on this workspace. With Live transcription off, no call audio leaves the computer for speech or the helper. |

### Audit the harness itself

1. Inventory the current actions, fault injection points, assertions, reset behavior, state coverage and skipped/no-op
   steps. Verify that generated steps actually reach the production pathways the test claims to exercise.
2. Review the expected-behavior model independently of implementation branches. Assert at each meaningful step and
   after controlled settling. A permanently unavailable provider should yield a visible safe failure, not an invented
   successful call. Liveness expectations require explicit recovery/fairness conditions and bounded deadlines.
3. Prove the suite detects defects using isolated deliberate mutations/fault hooks: permit an unauthorized dial,
   duplicate a submission, disable the test helper, remove a script/shared objection, suppress a practice phone,
   misroute a callback, hang up on a beep, or leave a call forever hidden/stuck. Every intended defect must trip its
   relevant assertion. Do not weaken the assertion to make a mutant pass; no mutations ship in the product.
4. Use causal valid carrier traces and separately labeled malformed/late/unknown events. Randomly duplicate/drop/reorder
   receipts, delay/loss carrier responses, interrupt persistence, restart the authority, reload the UI, overlap users,
   switch test modes, edit the master, repeat input, disconnect audio and expire leases/clock windows. Compose faults
   across many calls instead of resetting to a clean workspace after each short scenario.
5. Exercise both backend and frontend. Backend replay cannot prove trusted keyboard behavior, stale rendering,
   ringback/output routing or microphone stream lifetime. Add seeded browser journeys and local WebRTC/audio fixtures
   for those boundaries, with no use of the owner's real contacts or phones in random runs.
6. Retain fixed regression seeds in routine CI, and generate new seed ranges for extended sweeps. Never special-case
   the product for known test seeds. Record action/state/fault coverage and reject vacuous runs that never answer,
   deliver speech, invoke the helper, import a script, receive a callback or recover from a fault as intended.
7. Every failure names the invariant, seed, step, mode and request/call IDs, and prints an exact replay command.
   Save the ordered actions, injected payloads, time changes and observable effects. Replay must not depend on an
   unrecorded random choice or wall-clock race. Minimize failing traces into focused permanent regression tests.
   If artifact writing fails, preserve the original assertion and useful trace tail in test output.

### Required execution and evidence

- Audit the defaults before running; the current extended command is `uv run python scripts/check.py stress`.
  Its observed defaults are 512 outreach sequences and 64 test sequences at 200 steps, plus 48 shared-rep sequences
  at 80 events. These totals are a baseline, not evidence of coverage on their own.
- In addition, run at least 16 outreach and 16 test sequences at 2,000 steps, and 8 shared-rep sequences at 500 events,
  with fresh seeds and persistent state over each sequence. Record duration/resource use. Any necessary adjustment
  must be explicit in the receipt, not a silent reduction after a failure.
- Use disposable databases and a disposable relay for simulated multi-rep tests. Require the relay cases to execute;
  an unavailable fixture is a failed/incomplete gate, not a successful skip. No real calls during randomized testing.
- One implementation worker remains the rule. Parallel isolated test processes are permitted by rebuild section 12A;
  choose their count to avoid disrupting this shared Mac. They do not authorize parallel code writers or subagents.
- On a failure: reproduce the seed, explain the root cause, add a regression, fix it, rerun the affected checks, then
  complete the required sweeps against the final code. Capture the final exit status, actual pass/fail/skip totals,
  commit, seed ranges, step/event counts, coverage and paths to failure/replay artifacts.
- Review `scripts/calling_budget.py`: remove unnecessary detection complexity, report changes in states/timers/effects,
  and justify any increased budget. A larger stress suite must not conceal a more complicated calling design.

## 8. Execution order and release gates

1. **Baseline:** commit this plan and the rebuild-plan link as a docs-only commit; leave
   `docs/evidence/rebuild/2026-11-16-2.2.1-live-helper.md` untouched unless the owner says otherwise. Inspect dirty
   state and exact-head CI; fix existing red CI before starting feature work. Refresh the installed/public version and
   save rollback information, including a timestamped copy of the installed workspace database kept outside the
   repository. Then remove the `Agite_Customer_Mobiles_To_Keep_306` list with the app's Remove list control (owner
   instruction November 22; removal keeps call history and opt-outs). Do not disturb unrelated files or app data.
2. **Regressions and harness audit:** add failing tests for the defects in section 2 and independently validate the
   stress assertions. Keep each fix's tests coherent; do not start with a broad rewrite.
3. **Test context and helper:** implement section 3 through real backend and browser pathways. Verify a repeated
   base-script practice conversation, including objection then renewed interest, without lasting outreach effects.
4. **Manual calling/callbacks:** implement section 6, preserving trusted one-action dialing, continuous audio and
   usable hangup controls. Complete blocker exit tests for every changed readiness condition/message.
5. **Script generation/import:** implement sections 4 and 5, preserving the outside-folder runner boundary. Verify
   app generation and external generation accept the same path/content contract and that rep-to-rep import works.
5A. **Compliance:** implement section 9.3 C1 to C8 using the owner's section 9.4 answers; if one is still unanswered
   when this step starts, ask before implementing it. Every new blocker gets its exit and in-and-out test (G10). Verify
   an ordinary outreach number cannot pass any gate the decisions require, while owner test lines still work.
6. **Integrated verification:** run focused tests, WebKit and Chromium call journeys, local media tests, callback tests,
   fault/mutation checks and all required random sweeps. Exercise normal outreach as well as direct tests so isolation
   cannot make test calls pass while ordinary calling breaks. Record provider/model simulations as simulations.
7. **Commit and push:** run `uv run python scripts/check.py` while iterating and `uv run python scripts/check.py full`
   before every push. Commit coherent app changes on main; verify exact-commit GitHub CI. Keep external generator
   code/data, credentials, databases, recordings, build outputs and caches out of app commits. Record its local
   verification/version separately. No AI attribution in commits or release notes.
8. **Version and package:** select the next unused version above the current public release (expected 2.4.2 if 2.4.1
   is still latest). Patches remain 0 through 9, then bump the minor. Ship one release. If script generation is
   blocked while the calling, test-call and compliance work is complete and verified, ship those as 2.4.2 and the
   generation work as 2.4.3 rather than holding everything. Run package checks where required and build from
   the verified version commit. Preserve Mac, Windows and Ubuntu release assets and valid updater signatures.
9. **Signed candidate and live checks:** validate the exact signed candidate in disposable data first, including
   completed CSV import and two test destinations. Then run the affected live calling matrix before publication,
   as previously requested: ringback, two-way audio, base script/helper, voicemail audible through the beep with
   manual hangup, no automatic next call, repeated explicit calls and callback/script/ringing behavior. Add the
   short-call hold (End as voicemail about 3 seconds after answer keeps the line to 7), the voicemail cue and wording,
   an outreach dial refused outside the calling window with its exit, a requested callback allowed outside it, and
   Live transcription switched off for one call; after each live call, confirm no transcript text remains in the
   workspace database. The owner's part is about 20 minutes at the phones. Use only
   approved 203-821-1101 and 203-434-8547 destinations. Do not infer hardware or remote-PC success from mocks. Complete
   every independently executable check before requesting only the remaining human/audio observations.
10. **Publish and update:** delete the leftover 2.4.0 draft release (owner approved November 22). Publish the new
    release, verify it is the latest public release rather than a draft,
    verify the downloaded update manifest/artifacts and native update discovery, then install on this Mac through
    the signed updater under the owner's authorization. Preserve workspace data and automatic private credential
    storage; no new sign-ins or OS password prompts. Verify the installed version, launch/readiness, saved data and
    affected workflows. Record unsupported/unavailable remote hardware checks honestly.
11. **Closeout:** supply the public release/download link, app commit and green CI, installed Mac version, external
    generator location/instructions, regression/stress evidence and exact remaining limitations. If a required gate
    fails, keep the candidate unpublished and report that gate instead of claiming completion.

## 9. Legal compliance audit (November 22, 2026)

A software audit of outreach calling against federal and state rules as of November 22, 2026, from the code and from
primary sources where available, law-firm summaries otherwise. It is not legal advice. Counsel should review the
section 9.4 decisions and section 9.5 before outreach scales. Recheck anything marked unverified before relying on it.

### 9.1 What kind of calls these are

Live, rep-placed sales calls to businesses, one at a time, from imported lists, each dial a fresh rep action, with
no artificial or prerecorded voice on outbound calls. That is not an autodialer under *Facebook v. Duguid* (2021), so
the TCPA consent rules for autodialed or prerecorded calls do not apply. Both properties are the main legal
protection and stay tested invariants (section 7). Rules that still apply: the National Do Not Call Registry for
residential subscribers (which can include mobiles used personally; courts are split in 2026), the seller's internal
do-not-call list, calling hours, caller ID, caller identification, the Telemarketing Sales Rule ban on
misrepresentations (extended to business-to-business calls in 2024), state mini-TCPAs, and wiretap/recording laws
for live transcription.

### 9.2 Already compliant (keep and test)

- Voicemail drop and call recording were removed; queued playback, gather and record commands are cancelled and
  never sent (`telephony/workflows.py:360`). No text-to-speech on outbound calls; the callback greeting speaks only
  on calls the person placed to us.
- Calling window ends at 17:00 recipient local time, and every candidate zone must be inside the window
  (`compliance/policy.py`). C2 changes how candidate zones are chosen and what happens when none are known.
- Internal opt-outs block dialing, and recovery hangs up a live call to a newly suppressed number.
- Every Opening must say `{rep}` and `{seller}` (caller identification).
- CRM leads are screened against do-not-call lists, re-screened within 30 days (the 31-day rule), and held until
  screened (`leads/dnc.py`).
- 24-hour retry spacing plus the optional quick pair allows at most 2 attempts per lead per 24 hours, under the
  3-per-24-hours cap in Florida, Oklahoma and Maryland.
- Owner test lines bypass outreach gates only through `own_test_line`; opt-outs still apply to them.
- An `opt_out` outcome from any rep writes `opendialer_opt_out=true` to the HubSpot contact (`crm.py:802`), including
  CSV leads, because `opt_out` is a logged person outcome that matches or creates the contact. Test calls never write.
- Claim limits: generated blocks are accepted only with cited fact ids (`preparation/workflows.py:349`); the helper
  is told never to invent prices, schedules, guarantees or commitments and to follow Never say (`helper/live.py:21`).

### 9.3 Gaps and the changes in this release (with the owner's answers applied)

| ID | Finding | Change |
|---|---|---|
| C1 | CSV leads are never do-not-call screened by the app (`leads/dnc.py`; `dnc_screening` defaults off). | None in code (D2): the owner screens every supplied list before import. Remove the `Agite_Customer_Mobiles_To_Keep_306` list (section 8 step 1). |
| C2 | The start hour accepts 0 (`administration.py:967`); weekends and holidays are allowed; requested callbacks are held to the window; a lead with no state and no known area code cannot be dialed (`TIMEZONE_UNCERTAIN`); a supplied state is merged with the area code's zones instead of taking priority (`optimization/geography.py:106`). | Owner window: Monday to Friday, 9:00 to 17:00 in the prospect's time zone, no US federal holidays. Settings may narrow it, never widen it. Requested callbacks and inbound callbacks are exempt (D4). Zone source, the first that applies: a supplied time zone; the supplied state's zones; the phone's area-code state's zones; otherwise (non-geographic or unknown area code) every US zone from Eastern to Hawaii. When several zones remain, the window must hold in each, so an unsure lead is called only in the overlap (every US zone: about 2 p.m. to 5 p.m. Eastern, 3 p.m. to 5 p.m. in summer). Legal check (D6): federal 8 a.m. to 9 p.m.; strictest states are Texas from 9 a.m., Pennsylvania 9 a.m. to 7 p.m. with no Sundays or legal holidays (Act 47 of 2026, in force October 18, 2026), and Alabama, Louisiana, Mississippi and possibly Florida until 8 p.m. A 5 p.m. local end is legal everywhere; the real risk is calling too early in a western zone, which the overlap rule prevents. Limits: a mobile's area code can be stale (a supplied state or zone overrides it); Pennsylvania's own legal holidays beyond federal ones are not modeled. |
| C3 | Opt-outs are written to HubSpot by every rep (section 9.2), but read back only for contacts inside a synced HubSpot list (`leads/hubspot_sync.py:94`). A CSV number opted out by another rep is not checked. | Pull every contact with `opendialer_opt_out=true` (HubSpot contact search) into the local suppression list, matched by normalized phone: at app start, at each CSV import, before each calling session and every 15 minutes while calling. Queue and retry a failed opt-out write until HubSpot confirms it. If HubSpot is configured but unreachable for more than 9 business days (the FCC honoring limit is 10), outreach stops with a blocker whose exit is reconnecting HubSpot; after 1 day the Dialer shows a warning. The local list always applies. |
| C4 | When no rep can answer, the callback fallback speaks a message and hangs up; nothing is recorded. | Unchanged (D3). Missed callbacks already appear on the rep's desktop; reps follow up during business hours, and a stop request then is saved as an opt-out. D6 asks whether the fallback should take a voicemail. |
| C5 | No disclosure before live transcription; prospect audio streams to a third-party speech service and model. Transcript text is stored for 30 days (`privacy.py:19`) and read after the call by the summary, suggested outcome, next step, drop-off stage, talk-ratio timing, Review and call statistics. | No disclosure (D1). Settings switch, Live transcription and helper (default on); off means no call audio leaves the computer, nothing is transcribed, and the call screen says the helper and script following are off. Transcripts are not kept (D7): segments exist only while the call is live, feed only the live helper and script following, and are deleted with the call's helper tips when it ends; a sweep at startup and every hour deletes any a crash left behind; SQLite `secure_delete` is on so deleted text is overwritten; transcript text never reaches logs, receipts, HubSpot or test artifacts. Removed with it: the after-call summary (HubSpot and Review), suggested outcome, next step and talk-ratio timing, and the Review transcript. StatML is archived (rebuild D31); its one kept view, stage drop-off, moves from the summary to the last confident live script-follow position, which is stored without transcript text. Follow-up scripts and briefs that read the prior summary (rebuild D4) read the outcome and rep note instead. Rebuild D7 (30-day transcripts) is replaced. Residual risk accepted by the owner: the real-time interception question under California §631/632.7. |
| C6 | `# Voicemail` wording can omit who is calling and a callback number. | Validate `{rep}`, `{seller}` and `{callback_number}` (section 6.13). |
| C7 | Unsupported claims in AI-filled blocks or helper text (the TSR misrepresentation ban covers business-to-business calls since May 2024). | Already handled (section 9.2). Keep regression tests proving both guards survive this work package. |
| C8 | The short-call hold could leave a person in silence. | Hold only after End as voicemail; Escape is immediate (section 6.10). |

### 9.4 Owner answers (November 22)

- **D1 Disclosure:** no disclosure line and no per-call stop key. Add the Settings switch in C5.
- **D2 Do-not-call screening:** the owner personally screens every supplied list against the National Do Not Call
  Registry before import; no system-level check or enforcement. Remove the Agite mobile list.
- **D3 Shared opt-outs and fallback:** no keypress opt-out and no relay change; the fallback stays the same. Opt-outs
  are shared through HubSpot (C3). The owner notes outreach targets numbers not on the federal registry.
- **D4 Calling window:** yes, Monday to Friday 9 to 5 in the prospect's time zone; callbacks do not follow the rule.
- **D5 Voicemail cue:** section 6.12 defaults.
- **Missed-callback voicemail:** no change; the fallback records nothing and reps follow up on missed callbacks
  (default, matching "keep it the same" in D3).
- **D6 Time zones:** decide from supplied data first, then the area code, and only when truly unsure use the
  every-US-zone overlap; never block a lead just for being unsure (C2).
- **D7 Transcripts:** not kept; used only for the live helper and script following (C5). The owner was told what this
  removes.

### 9.5 Outside the software (owner or counsel)

- The owner's list screening (D2): screen against the National Do Not Call Registry before import, and re-screen any
  list still being called every 31 days; the federal safe harbor relies on a registry copy no older than 31 days.
  Keep the screening date and source for each list. Registry access is the seller's own (the first five area codes
  are free) or through a vendor.
- Counsel review of the D1 and D3 answers and of state telemarketer registration duties (several states require
  registration; business-to-business exemptions vary).
- A written internal do-not-call policy, available on request, and rep training on it (47 CFR 64.1200(d)). The app
  can supply a template; the owner adopts it. Honor each request for 5 years; OpenDialer keeps opt-outs permanently.
- California privacy notices if the seller meets CCPA/CPRA thresholds (the business-to-business exemption ended
  January 1, 2023).
- FCC September 30, 2026 order (effective November 25, 2026, per secondary sources): telemarketing opt-outs still
  reach all of the caller's telemarketing; the 10-business-day honoring limit stands while a shorter one is proposed.

Sources: [Telnyx short-duration calls](https://support.telnyx.com/en/articles/1130707-what-are-short-duration-calls),
[Telnyx abandoned-call surcharge](https://support.telnyx.com/en/articles/12805746-surcharge-for-high-abandoned-call-rates),
[FTC TSR 2024 final rule](https://www.federalregister.gov/documents/2024/04/16/2024-07180/telemarketing-sales-rule),
[FCC September 2026 TCPA amendments (Crowell)](https://www.crowell.com/en/insights/client-alerts/fcc-officially-adopts-amendments-to-tcpa-rules),
[SB 690 and CIPA §631 (Recording Law)](https://www.recordinglaw.com/news/california-sb-690-cipa-pen-register-carve-out/),
[CIPA case watch October 2026](https://consentpixel.com/blogs/cipa-case-watch-october-2026/),
[Do-not-call and cell phones, 2026 split (Duane Morris)](https://blogs.duanemorris.com/classactiondefense/2026/09/21/florida-federal-court-holds-that-the-tcpas-do-not-call-provisions-do-not-apply-to-cell-phone-users/),
[State calling hours 2026 (vendor summary, unverified)](https://subscriberverify.com/blog/tcpa-quiet-hours-by-state-2026),
[Pennsylvania Act 47 of 2026 (Mac Murray & Shuster)](https://mslawgroup.com/pennsylvania-overhauls-its-telemarketing-law-what-changes-on-october-18-2026/),
[Texas calling hours (Texas State Law Library)](https://sll.texas.gov/faqs/how-late-can-telemarketers-call).

## 10. Checkpoint

- November 22, 2026: scope written down at the owner's request and linked from the active rebuild plan.
- **Status: planning complete; execution explicitly not started.** No product or generator code changed, no tests
  run for this planning step, no calls, commits, pushes, release or update performed.
- The investigation results in section 2 are prior evidence only. Every execution gate remains open.
- Next action after the owner says to begin: reread both plans, verify baseline/CI and start with the regression tests
  and stress-harness audit. Update this checkpoint with verified results, failures and the exact next action after
  each coherent implementation step; never mark a gate complete from an intention or a running process.
- November 22, 2026 (later): the owner approved the recommendations now in sections 4 to 9, asked for advisory
  voicemail guidance (6.12) and a legal compliance audit (section 9). Verified read-only: `main` `5466b36` Verify and
  Signed release green; latest public release 2.4.1; leftover 2.4.0 draft; this Mac runs 2.4.0 with the Agite
  workspace (306 leads, 293 scripts ready); background computer-use access to OpenDialer granted. Telnyx short-call
  and abandoned-call rules read from Telnyx support pages. Open owner decisions: D1 to D5 (section 9.4) and the
  question about the Agite CSV list. **Execution still not started**: no product code, tests, calls or commits.
  Next action once the owner says begin: section 8 step 1.
- November 22, 2026 (owner answers): D1 no disclosure, Settings switch instead; D2 owner screens supplied lists,
  no system check, remove the Agite mobile list; D3 no keypress opt-out or relay change, share opt-outs through
  HubSpot; D4 weekday 9 to 5 in the prospect's zone, callbacks exempt; D5 cue defaults. Verified read-only: HubSpot
  opt-out write exists for every rep (`crm.py:802`) but read-back covers only synced-list contacts (C3 fixes it);
  claim guards already exist (C7); transcripts are kept locally for 30 days (`privacy.py:19`). Open: D6. **Execution
  still not started.** Next action once the owner says begin: section 8 step 1.
- November 22, 2026 (time zones and transcripts): D6, decide the zone from supplied time zone, then state, then
  area code, and only when truly unsure call in the every-US-zone overlap instead of blocking; Pennsylvania's Act 47
  hours (9 a.m. to 7 p.m., no Sundays or holidays, in force October 18, 2026) confirmed as the strictest end. D7,
  transcripts are never kept and feed only the live helper and script following; this removes the after-call
  summary, suggested outcome, next step, drop-off stage and talk-ratio timing (owner informed). Missed-callback
  voicemail: no change. **Execution still not started.** Next action once the owner says begin: section 8 step 1.
