# Call records, prime time and script tests: plan

Written November 28, 2026 and approved by the owner the same day ("implement this plan end to end"), after a legal
audit (section 4). **Status: built and released in 2.5.0. Open: Telnyx retention (C3), the relay migration on the live project, live calls.** Read this alongside [the rebuild plan](REBUILD_PLAN.md)
and its Status section. Build rules are section 12A of that plan: one worker, on `main`, acceptance tests first, fast
check after each change, full check before each push. Calling rules (G10): any new blocker declares its exit in
`calling/blockers.py` and has an in-and-out test.

## TLDR

- **Keep calls to learn from them.** Your side (audio and transcript) is always kept. The prospect's side is kept only
  when both their listed state and their area code's state allow one-party consent, or after you say "this call is
  recorded" and press `R`. Audio and transcript are kept as a timed pair, until you delete them.
- **The live helper stays on every call.** Live transcription keeps running for the helper and script following; only
  what is kept changes. A Settings switch offers the stricter Nooks behavior (no live prospect transcription in the
  stricter states until `R`).
- **Prefer prime time.** The queue always calls a lead who is in the best local time band right now, and retries land
  in the lead's next prime window. Ranking only; calling hours stay the hard limit.
- **No accidental script jumps.** Nearby moves stay automatic; a far jump needs a distinctive phrase said twice, or a
  click.
- **Script variants** with written hypotheses, assigned by code so results are a fair comparison, tracked on every call.
- **Call numbers and insights** (talk-to-listen, monologues, response gap, time to pitch, pace), compared in Review and
  exported with the audio and transcripts for analysis anywhere.
- **Compliance fixes from the audit:** delete the unused carrier transcription path, hold numbers outside the 50 states
  and DC, show what is being recorded, never build voiceprints, filter speaker bleed, confirm Telnyx keeps nothing.
- About 14 to 17 working days in 9 phases, one signed release at the end.

## 1. Owner direction

November 24 and 28, 2026:

- Store enough call logs (transcripts and audio, plus what is derived from them) to get real insights.
- Call at the best times. The ideal time is the same for everyone in their own time zone; calling should prefer prime
  time.
- No accidental large script jumps.
- A/B testing: several script possibilities, with hypotheses for when to use them, and the data tracked.
- Keep audio and transcripts in pairs, downloadable for analysis elsewhere, with results by variant and anything else
  useful.
- Legal: defer to what other dialers do. The app does not police its users, but it must not allow anything outright
  illegal (automatic dialing, calling outside legal hours and the like).
- Recording only the rep's side is acceptable. Keep the live helper; keep the prospect's audio and transcript only when
  sure it is allowed.

## 2. What exists today

| Area | Today | Where |
|---|---|---|
| Script following | Only the rep's finished speech is matched, but against every option in the script. A score of 0.72 (with a 0.10 lead over the next) moves the panel anywhere. A fully matched 3 to 4 word phrase scores 1.0, so a generic line can jump the panel across the script. | `script_follow.py:104`, `privacy.py:372` |
| Speaker bleed | November 15: a test call's transcript had nearly the same words on both tracks. A phone near the Mac, or speakers instead of a headset, puts the prospect's voice into the rep's mic, where it counts as the rep. | Rebuild plan Status, November 15 |
| Live transcription | Shared calling (every real setup): the desktop sends each side as 16 kHz mono PCM to the local backend, which streams it to Telnyx speech-to-text with the Soniox engine. Text returns to the app for script following and the helper. The helper sends text to Cerebras through OpenRouter with zero retention and no data collection. | `live_transcription.py:90`, `providers/telnyx_stt.py`, `providers/openrouter.py:16` |
| Carrier transcription path | Used only when no relay is configured, which no real setup has. It asks Telnyx to transcribe each leg; the relay replaces those events with `relay.unexpected_transcription` and saves no text (tested). | `telephony/workflows.py:200`, `telephony/ingress.py`, `supabase/functions/telnyx-webhook/receiver.ts:194` |
| Transcripts | Text is deleted when the call ends (3-hour backstop). Who spoke when (`side`, `start_ms`, `end_ms`) and the matched script part stay. | `privacy.py:19`, `privacy.py:417`, `privacy.py:500` |
| Audio | Not stored. | |
| Telnyx retention | Not confirmed. Soniox states it stores no audio or transcripts unless asked and never trains on them. The app has an unchecked readiness item for this evidence. | `administration.py:129` |
| Calling window | Monday to Friday, 9:00 to 17:00 in the prospect's zone, no federal holidays; an unsure zone is called only in the every-zone overlap. | `compliance/policy.py` |
| Non-US +1 numbers | Canadian, Caribbean and US-territory area codes have no state, so they are treated as unknown US numbers and dialed in the US overlap hours. | `optimization/geography.py:69`, `service.py:740` |
| Queue order | Due callbacks and requested contacts, then never called, then longest waiting. Time of day plays no part. | `scheduling/policy.py:29`, `scheduling/queue.py:292` |
| Retries | A fixed 24 hours, so a retry lands at the same local hour the next day. | `compliance/policy.py:89` |
| Paths | AI picks one path per lead. That is targeting, not a test: the leads in each path differ. | `preparation/templates.py:54` |
| A/B remnant | A comparison assignment hook in the queue, switched off while prepared scripts are required. | `scheduling/queue.py:470` |
| Export | Review exports a ZIP: `calls.csv`, `script-events.csv`, `script-structure.csv`, `stage-statistics.csv`. | `call_statistics.py` |
| Volume | 29 call attempts, 6 right-party conversations, 2 meetings (November 24). | Installed workspace |

## 3. Market and data

**Recording and transcription in states that need every party's consent**

| Product | What it does |
|---|---|
| Orum | Admin toggle. Record all calls with rep-only audio, or limit recording by area code (a pasteable two-party list). States that the company is responsible for complying. |
| Nooks | Skip recording for two-party area codes (then no live or post-call transcription either), or record and transcribe only the rep for them. Preset, editable state list; no guarantee of accuracy. Optional auto-delete after a chosen number of months, off by default. |
| Outreach | A consent dialog before each recorded call; the rep confirms or picks Don't record. |
| HubSpot | Account-wide recording, on by default. Off for two-party area codes until the rep clicks Record and confirms consent. |
| Salesloft | One-sided recording for location compliance, plus a standard recording announcement. |

No product silently records both sides everywhere. State lists differ between vendors (10 to 16 states), so this plan
uses the broad list and makes it editable. Nooks treats live transcription like recording; this plan keeps live
transcription for the helper by default (P2) and offers Nooks' behavior as a switch.

**Best time to call.** Sources disagree on afternoons. Orum (over a billion calls) reports connects peaking at 10 a.m.
local and declining after 2 p.m.; older studies favor 4 to 5 p.m. The consistent finding is a strong late morning and a
weak lunch hour. Hence a fixed default curve that learns from this app's own dials.

**Talk patterns (Gong).** Successful cold calls had longer rep monologues (about 37 seconds against 25) and shorter
prospect monologues. A reference for phase 6, not a target.

## 4. Legal audit (November 28, 2026)

Not legal advice. The standard is the owner's: the app must not allow anything outright illegal; everything else
follows what other dialers do, and the user is responsible for how they use it.

| Area | Finding | Action |
|---|---|---|
| Automatic dialing | Every call needs a fresh rep action, one at a time (D5, G11). Not an autodialer under *Facebook v. Duguid*; more conservative than parallel dialers. | Keep the invariant and its tests. |
| Recorded or AI voice | None on outbound calls; the AI only advises the rep. | Keep. A recording disclosure is spoken by the rep, never played. |
| Calling hours | Weekdays 9 to 5 in the prospect's zone, no federal holidays, unsure zones in the overlap; inside every state's legal hours. | Keep. Prime time only reorders inside the window. |
| Call frequency | At most 2 attempts per lead per 24 hours, under the 3-per-24-hours cap in Florida, Oklahoma and Maryland. | Keep. |
| Do-not-call | Internal opt-outs are permanent and shared through HubSpot. CSV lists are screened by the user. | Keep; screening stays the user's job, as with other dialers. |
| Caller ID and identification | Owned numbers that can be called back; every Opening names `{rep}` and `{seller}`. | Keep. |
| Misrepresentation | AI script lines must cite facts; the helper is told not to invent prices or promises. | Keep the regression tests. |
| Recording | Rep side always; prospect only when sure or after `R` (P1). Matches Orum and Nooks, plus the Outreach and HubSpot consent prompt. | Phase 3. |
| Live transcription of the prospect | Runs in every state. Real-time AI transcription by a vendor is the theory in current California suits (CIPA §631 and §632); not storing the text does not change that theory. Courts are split and nothing decided against a sales dialer was found. Same risk the owner accepted November 22. | Keep for the helper (owner decision, P2); add the Nooks switch; keep every vendor on zero retention (C3). |
| Carrier transcription path | Unused; would send prospect audio to Telnyx for text the relay throws away. | Delete (C1). |
| Numbers outside the US | Canada, the Caribbean and US territories are dialed as unknown US numbers; their hours, holidays and recording laws are not modeled. | Hold them (C2). |
| Recording the rep | Some states (New York, for one) require employers to give notice of electronic monitoring. | Visible indicators (C4); the employer's written notice is the user's job. |
| Voice analysis | Voiceprints and speaker identification are biometric identifiers under Illinois BIPA and similar laws. | Never build them (C5). |
| Speaker bleed | "Rep side only" contains the prospect if their voice reaches the rep's mic. Same physics for every dialer. | Echo cancellation, headset guidance, bleed filter (C6). |
| Exports | Contain voices and personal data. | The user's responsibility; consent basis travels with each call. |

The user's responsibilities, as with every dialer, are shown in a short Settings note: screening lists against
do-not-call registries, state telemarketer registration, a written do-not-call policy, telling the prospect before
pressing `R`, employee monitoring notice, and what they do with exports. The state list is a default, not legal advice.

## 5. Decisions

Owner approved November 28, 2026. They become rebuild plan decisions in phase 0.

| # | Decision | Replaces or changes |
|---|---|---|
| P1 | **Keeping recordings.** The rep's audio and transcript are kept on every answered call (one switch turns it off). The prospect's audio and transcript are kept only when sure: the supplied state (if any) and the area code's state are both outside the stricter list. Otherwise only the rep's side is kept, unless the rep says the call is recorded and presses `R`, which keeps both sides from that moment. Stricter list, editable: CA, CT, DE, FL, IL, MD, MA, MI, MT, NV, NH, PA, WA. Unknown states, toll-free and non-US numbers count as not sure. No rep-state setting: like Orum and Nooks, the rule uses the prospect's location. | Work package C5 and D7 (transcripts never kept). D1 (no disclosure) stays the default; `R` is the rep's choice. |
| P2 | **Live transcription and the helper stay on every call.** A Settings switch, "Transcribe the prospect live in stricter states" (on by default), offers Nooks' behavior: when off, the prospect's audio goes to speech only on sure calls or after `R`. The existing Live transcription and helper switch still turns all of it off. | None; adds a switch. |
| P3 | **Recording is local.** The backend writes the PCM it already receives. No Telnyx record command is ever sent, so the stress invariant "no speak, playback, gather or record command on an outbound leg" stays. Audio files are encrypted with the workspace's local key. | Stress invariant "no transcript after the call" becomes "nothing kept that P1 does not allow" |
| P4 | **Retention follows the market.** Kept until deleted. Optional auto-delete after a chosen number of months (off by default, as in Nooks). Delete one call or all; an opt-out deletes that phone's recordings. Settings shows the disk space used. | |
| P5 | **Prefer prime time.** One local-time curve for every prospect. The queue always picks from the best band available now, falling through to lower bands; it never holds a lead for prime time. Hours, attempts, opt-outs and due callbacks stay ahead of it. | Rebuild section 6 order and D31's plain order |
| P6 | **Script following has a distance rule.** Nearby moves stay automatic; far jumps need distinctive, repeated evidence or a click. | D36 ("moves the script panel to a confident match") |
| P7 | **Variants are assigned by code.** A stable random draw per lead before generation, recorded with the script and every call. Paths stay AI targeting and are labeled as not a fair comparison. | D13 (manual A/B tests go) |
| P8 | **The app blocks only what is outright illegal or unmodeled.** No automatic dialing, no played voice, legal hours, opt-outs, and no numbers outside the 50 states and DC. Everything else is a setting with a plain note. | Work package section 9.5 counsel review is no longer a gate for these features |

## 6. Phases

### Phase 0: plan and tests first (half a day)

1. Record P1 to P8 in the rebuild plan's decisions and Status, and mark work package C5 and D7 as replaced.
2. Write the acceptance tests for each phase before its code: `test_compliance_fixes.py`,
   `test_script_follow_distance.py`, `test_call_recording_rules.py`, `test_prime_time_queue.py`,
   `test_script_variants.py`, `test_call_metrics.py`, `test_call_export.py`, plus any new blocker in
   `test_blocker_exits.py`.

### Phase 1: compliance fixes (1 day)

1. **C1. Delete the carrier transcription path**: the `transcription_start` commands and the `leg_inbound` layout in
   `telephony/workflows.py`, the `call.transcription` handling in `telephony/ingress.py`, the `webhook` choice in
   `calling/dial.py`, and their tests. A workspace without a relay gets no live transcription and says so. The relay keeps
   dropping transcription events, and its test stays as a guard.
2. **C2. Hold numbers outside the 50 states and DC** (Canada, the Caribbean, US territories), with the reason shown in
   Leads and the Dialer's queue reasons and the exit "correct the number or remove the lead". Toll-free numbers stay
   callable in the US overlap hours. If the hold surfaces through `calling/blockers.py`, it declares its exit and gets
   its test.
3. **C3. Vendors keep nothing.** With the owner, read Telnyx's data-retention setting in the portal, set it to the
   shortest option, and record the evidence in the existing readiness item (`transcription_privacy_reference`). Keep
   the OpenRouter route on zero retention with no data collection and its test.
4. A regression test proves transcript text still never reaches HubSpot, logs, receipts or the relay.

### Phase 2: no accidental script jumps (1 day)

1. Distance. Parts within reach (the current part, the next two in Markdown order, the previous one) move on today's
   thresholds.
2. A far jump needs both: a matched phrase of 6 or more words that occurs in no other part (distinctiveness computed
   once per frozen script), and two consecutive rep segments matching the same far part. Otherwise the panel stays and
   shows a "Pricing ↓" chip that jumps on click.
3. Parts reached from anywhere by nature (objections, screening, voicemail) use the distinctiveness rule without the
   repeat rule.
4. **C6. Bleed filter.** When the prospect is transcribed: a rep segment that overlaps a prospect segment in time with
   70% or more of the same words is ignored for following, the helper, metrics and the kept rep transcript. When the
   prospect is not transcribed: a rep segment that falls inside the prospect's voice activity at a lower level is
   ignored the same way. Audio bleed cannot be removed after the fact.
5. Ask the WebRTC client for echo cancellation, noise suppression and gain control explicitly where the Telnyx SDK
   accepts constraints. Settings and the test call recommend a headset.

Done when replayed calls show: a generic phrase never moves the panel more than one part; a distinctive far phrase said
twice does; the chip works; both bleed cases are dropped.

### Phase 3: call records (4 to 5 days)

1. Spike (half a day): encode 16 kHz PCM to Ogg Opus in the frozen backend on Mac, Windows and Linux (candidate:
   libsndfile through `soundfile`). Fallback: FLAC, about 5 times larger.
2. Capture. The audio socket appends each side's frames to a per-call file with its first-frame offset. Local capture
   runs even when Live transcription is off (nothing leaves the computer then). At call end the two sides are aligned
   and encoded into one stereo file: rep left, prospect right (silent where not kept). About 0.2 MB per call minute.
3. Consent. One pure function decides what is kept and what is transcribed live, from the supplied state, the area-code
   state, the stricter list, the P2 switch and the `R` time. Its result is saved on the call: `rep_only`,
   `both_one_party`, `both_from_consent_at_ms`, or `rep_only:<reason>` (for example `rep_only:CA`, `rep_only:unknown`).
4. `R` on the call screen: "I told them this call is recorded". Keeps the prospect's audio and transcript from that
   moment; starts their live transcription if the P2 switch had held it. Inbound callbacks follow the same rule by the
   caller's number.
5. Transcripts. Kept segments follow P1. The live helper sees what is transcribed during the call; text that cannot be
   kept is deleted at call end as today, with the startup and hourly sweep and `secure_delete`.
6. **C4. Indicators.** The call screen always shows what is happening: "Recording your side", "Recording both sides",
   "Prospect audio not kept: CA", "Prospect not transcribed: CA" (P2 switch off). Settings shows the same rules and the
   user-responsibility note from section 4.
7. Storage. Audio files live in the workspace's `recordings/` folder, encrypted with the workspace's local key file.
   Metadata and transcript text stay in SQLite. Retention (P4) runs in the maintenance tick.
8. Deletion: per call, all calls, auto-delete after N months when set, and on an opt-out every recording of that phone.
9. Settings rows: Record my side (on), Transcribe the prospect live in stricter states (on), Stricter states (Advanced,
   editable), Auto-delete recordings (off), disk space used.
10. Review: play a call with the transcript highlighted in sync; shows what was kept and why.
11. Test calls are recorded under the same rules, marked test, and left out of insights.

Done when replay tests cover each P1 and P2 rule, `R` before and after answer, unknown, toll-free and non-US numbers,
a crash mid-call, retention and opt-out deletion, and the reworked "nothing kept that P1 does not allow" stress
invariant passes.

### Phase 4: prefer prime time (2 days)

1. Default curve, weekdays, lead's local time, half-hour bands:
   - Prime: 9:30 to 11:30 and 16:00 to 17:00.
   - Good: 9:00 to 9:30, 11:30 to 12:00, 13:30 to 16:00.
   - Low: 12:00 to 13:30.
2. Lead zone. A stored queue column with the lead's resolved zone, or `unsure` with its candidate zones. An unsure
   lead is scored by its worst candidate zone.
3. Order: due callbacks and requested contacts first (unchanged); then the best band available now; inside a band,
   never called, then longest waiting. The per-zone band is computed in Python (a handful of zones) and applied in the
   SQL order, so the right leads reach the candidate page. When the best band is empty, the next band is called; a lead
   is never held for prime time.
4. Retries are scheduled into the lead's next prime window, alternating morning and afternoon blocks, never sooner than
   the 24-hour minimum.
5. Learning. After about 1,000 dials, each band's score blends the default with this workspace's own human-answer rate
   for that band, weighted by the dial count (for example n / (n + 200)). It changes ranking only.
6. Dialer status line: "Central leads · 10:14 there · prime".
7. Several reps: each queue uses the same curve on its own leads. Check whether two reps who import the same CSV can both
   call one person; add a guard through the relay's sticky-caller record if so.

Done when fixed-clock tests pass: at 12:30 Pacific on a Tuesday, Eastern and Central leads go first; at 7:00 Pacific,
Eastern leads are prime; a due callback still goes first; retries land in prime windows; hours and holidays still hold.

### Phase 5: script variants (2 to 3 days)

1. Syntax, parallel to `# Paths`:

   ```
   # Variants
   - direct-open (50%): Hypothesis: naming the trigger first gets past the opener more often.
   - permission-open (50%): Hypothesis: asking for 30 seconds cuts early hang-ups.
     When: hiring_sellers
   ```

   Options are tagged `[variant: name]`; untagged text is shared. One active test per master; shares must total 100.
2. Assignment by code: a stable hash of the lead and test name, drawn before generation. `When:` (a trigger type, path or
   lead field) limits the test to matching leads; others get the shared text.
3. Generation keeps only the assigned variant's options, in the app and in `generate-scripts` (one pipeline). The
   portable CSV gains a `script_variant` column; import keeps it.
4. Saved with the script, in the call snapshot, and in `calls.csv` and the stage statistics.
5. Review: per variant, calls, conversations, past opener, reached pitch and meetings, with ranges and "not enough data"
   below the minimum. Paths show alongside, labeled as not a fair comparison.
6. The parser and editor explain variant errors the way they do for paths.

### Phase 6: call metrics (1 to 2 days)

1. From timings on every call, rep and prospect: talk-to-listen, longest monologue, response gap, interruptions, time to
   pitch, time per script part. Prospect timing comes from voice activity, so it works without their words.
2. From kept transcripts: words per minute, questions asked.
3. From the rep's own audio: pitch range and loudness (a monotone signal). Prospect tone only where their side is kept.
   No emotion classifier.
4. **C5. No voiceprints.** Metrics are a fixed list of named numbers; no speaker embeddings, voice identification or
   voiceprint is computed or stored. A test checks the stored metric fields against the list.
5. Stored as numbers on the call; past calls get the timing metrics from existing metadata.

### Phase 7: insights and export (2 days)

1. Review compares results by variant, path, local time band, list, trigger and metric bucket (for example longest
   monologue over 30 seconds), with counts, ranges and a minimum-sample guard.
2. Export to a chosen folder (bundles get large):
   - `calls.csv`: every dimension above, plus rep, number, zone, local hour, outcome, duration, metrics and what was
     kept and why.
   - `calls/<call_id>/audio.ogg` (rep left, prospect right) and `transcript.jsonl` (side, start and end ms aligned to the
     audio, text, matched part, score), plus a readable `transcript.txt`.
   - The existing script events, structure and stage statistics files.
   - Filters: date, list, outcome, variant, conversations only, include test calls.
3. Each rep exports from their own computer in the same format with a `rep_id`, so a team merges files outside the app.
4. Later and optional: an after-call AI pass over kept transcripts, on the zero-retention route, that tags objections and
   whether a meeting was asked for.

### Phase 8: release (1 day plus live calls)

1. Full local check and the standard stress sweep with the updated invariants.
2. One signed, published release.
3. Live calls to the owner's test line (203, Connecticut, so not sure) with the owner on the line: "Prospect audio not
   kept: CT" with the helper working; `R` keeps both sides from that moment; with the P2 switch off, no prospect
   transcript until `R`; a far phrase does not move the panel; the Dialer shows the prime-time reason. The sure path
   is proven by replay tests.
4. Update this Mac only on the owner's request.

## 7. Volume and honest limits

- Opener test (40% against 55% past the opener): about 170 conversations per variant.
- Meeting-rate test (3% against 5%): about 1,500 conversations per variant.
- Own time curve: about 1,000 dials.
- At the 3,000 dials a week this plan was sized for, opener tests resolve in one to two weeks; meeting tests take months.
  Test early stages, one test at a time.
- Prospect-side insights (their words, tone) come only from kept calls; timing metrics cover every call.
- Storage: assuming 300 conversations a week at 3 minutes, about 160 MB of audio a week, about 8 GB a year if nothing is
  deleted.

## 8. Risks

| # | Risk | Handling |
|---|---|---|
| K1 | Keeping the prospect's audio where all-party consent applies. | Kept only when sure; broad, editable list; unknown counts as not sure; `R` only after a spoken disclosure; indicators. |
| K2 | Live transcription of the prospect in the stricter states (owner-accepted, November 22 and 28). | Not kept unless sure or `R`; vendors on zero retention; the Nooks switch for users who want it off. |
| K3 | "Rep side only" contains the prospect through speakers. | Headset guidance, explicit echo cancellation, bleed filter for text and metrics. |
| K4 | Exports carry voices and personal data off the computer. | The user chooses the folder; what was kept and why travels with each call. |
| K5 | Telnyx keeps speech audio or text. | C3 confirms and sets its retention before release. |
| K6 | Prime-time preference starves leads in one zone. | It only ranks; lower bands are called when the best band is empty. |
| K7 | Small samples read as results. | Ranges, minimum samples, "not enough data". |
| K8 | Opus encoder not available in a frozen build. | Phase 3 spike first; FLAC fallback. |

## 9. Owner inputs

- [x] Recording model: rep side always; prospect only when sure or after `R` (November 28).
- [x] Keep the live helper on every call (November 28).
- [x] Prime time: prefer, never hold (November 28).
- [x] Legal: follow other dialers; block only what is outright illegal (November 28).
- [x] Retention: keep until deleted, optional auto-delete, as in Nooks (November 28, following the market).
- [ ] Telnyx data-retention setting, read in the portal together (C3).
- [ ] Answer the test line for the phase 8 live calls.

## 10. Sources

- [Orum: call recording settings and two-party consent](https://support.orum.com/en-US/orum/article/mg6SiQI7-call-recording-settings-and-two-party-consent)
- [Nooks: recording calls](https://nooks.help.usepylon.com/articles/7521860980-recording-calls-on-nooks)
- [Nooks: recording retention and deletion](https://nooks.help.usepylon.com/articles/2726404221-recording-retention-deletion-overview)
- [Outreach: call recording laws](https://support.outreach.io/hc/en-us/articles/220339488-Call-Recording-Laws-Regulations-US-and-International)
- [HubSpot: call recording laws](https://knowledge.hubspot.com/calling/what-are-the-call-recording-laws)
- [Salesloft: call recording](https://salesloft.com/platform/call-recording-software)
- [Telnyx: recording start](https://developers.telnyx.com/api-reference/call-commands/recording-start)
- [Telnyx: data residency and retention](https://developers.telnyx.com/docs/inference/data-residency)
- [Soniox: security and privacy](https://soniox.com/docs/stt/security-and-privacy)
- [Orum: cold calling in 2025](https://www.orum.com/blog/cold-calling-2025)
- [Gong: talk-to-listen ratio](https://www.gong.io/resources/labs/talk-to-listen-conversion-ratio/)
- [Bloomberg Law: AI notetaking and wiretapping](https://news.bloomberglaw.com/litigation/ai-notetaking-poses-wiretapping-discovery-and-ethical-pitfalls)
- [Holland & Knight: GenAI wiretap suits, August 2026](https://www.hklaw.com/en/insights/publications/2026/08/the-wave-continues-to-build)

## Checkpoint

- November 28, 2026: plan written from a read-only review of the code and public vendor documentation.
- November 28, 2026: owner approved it; legal audit added (section 4) with fixes C1 to C6; owner answers recorded
  (section 9). No code changed, no calls placed. Next: phase 0.
- November 28, 2026: phases 0 to 3 done on `main` (`657a33f`, `0e01b44`, `a7c22dd`): carrier transcription removed and
  numbers outside the US held (C1, C2); far script jumps need a distinctive phrase twice, speaker bleed is ignored
  (P6, C6); recordings and transcripts kept under P1 to P4 in `app/call_records.py` and `app/calling/audio_tracks.py`,
  with the Dialer indicator and R key, the Settings card and the Review player. Next: phase 4 (prime time).
- November 28, 2026: phases 4 to 7 done on `main`: prime-time ordering, prime retries and the cross-rep 24-hour check
  (`scheduling/prime_time.py`, relay migration `20261128120000_recent_prospect_guard.sql`, not yet applied to the
  live relay); script variants (`preparation/templates.py`); call numbers (`call_metrics.py`); comparisons and the
  export (`insights.py`, `call_export.py`); Review Insights card; WebKit spec `call-records.spec.ts`. Version 2.5.0
  prepared. Next: full check, stress sweep, push, CI, signed release; owner items: Telnyx retention (C3), applying the
  relay migration, live calls.
- November 28, 2026: full local check passed (backend, frontend, Rust, WebKit browser suite); the stress sweep passed
  624 of 624 runs from seed `3521543373` (512 single-rep runs of 200 steps, 64 test-phone runs, 48 two-rep relay runs;
  the coverage ledger shows every run dialed, answered, held and saved); all seven planted defects are caught. CI on
  `c069142` is green on all eight jobs (two WebKit reruns for unrelated flaky tests: `csv-paths.spec.ts` pause click
  and `followthrough.spec.ts` focus). The packaged backend now ships numpy so it can encode call audio. Signed 2.5.0
  released from `c069142`. Not done: C3 needs the owner's Telnyx portal sign-in; the relay migration
  `20261128120000_recent_prospect_guard.sql` is not applied to the live project (the app works without it and starts
  checking once it is); live calls to the test line need the owner on the line; this Mac is not updated.

