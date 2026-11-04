# Numbers and connect rate plan

Updated November 4, 2026. Software evidence only so far: no real number has been bought and no real call placed.

## Goal

- Numbers bought in the app set themselves up, and numbers already in Telnyx are easy to add.
- Everything on the number and calling side is handled to best practice. Lists, timing and rep behavior stay out of scope.
- Reach a sustained live connect rate we can measure honestly.

## Target

**Live connect rate = attempts reaching a live person with working two-way audio / all initiated outbound attempts.**
An immediate "not interested" counts. Failed, blocked and unanswered attempts stay in the denominator.

| Case | Rate | At 180 attempts a day |
|---|---|---|
| Budget | 12% | about 22 connects |
| Target | 15% | 27 connects |
| Stretch, only once demonstrated | 20% | 36 connects |

These are planning judgments, assuming US B2B, verified direct numbers, healthy cadence and one call per rep. Infrastructure
cannot guarantee them. Public reference points: Gong 5.4% average and 13.3% top quartile over 300M+ calls; Rasa 13 to 16%;
Park Place about 21%. Definitions and dialing modes differ, so these show what is feasible, not what to expect. Vendor lifts
overlap and must not be added together.

Report separately: network answers, live human pickups, usable live connects (the main number), intended-prospect connects,
screening, voicemail, IVR, blocked, failed, conversations and inbound callbacks. Voicemail, screening assistants and callbacks
never count as connects.

## Done

Shipped in 0.8.3 and 0.8.4:

- Buying or adding a number confirms its route, turns recording off, applies the spend cap and caller name, and starts warmup.
- Telnyx numbers are listed and added one at a time, with a warning before moving one off another Telnyx app. Pause excludes a number.
- One workspace test call with no expiry. Callers on no lead hear a greeting with the business name.
- City search with each number's city, tried one area code at a time. Same metro first, one number per lead.
- Telnyx iPhone-aware detection. Only a voicemail beep hangs up an ordinary attempt. The final attempt stays on the line.
- The rep is bridged on answer, so detection does not hold up live audio.
- Clear refusals, one transcription switch, optional caller name, $25 daily spend cap (set on the real account).

Telnyx account: the owner reports it is now Verified. Not yet proven on a real call that the hourly cap and the automated notice
are gone. Balance $15. No numbers owned.

## Close-out order

### A. Owner setup

- [x] Confirm Telnyx shows Verified in the console. Check real calling limits and announcements during live proof.
- [x] Decide number health scope. Use Free Caller Registry, manual reports and observed delivery. Skip Telnyx Number Reputation.
- [ ] Fund the calling balance and decide auto-recharge.
- [ ] Set the callback destination and optional caller name. Review the applicable transcription and do-not-call rules.

### B. Release 0.8.5

- [ ] Request Telnyx voicemail AMD with the rep bridged on answer. End ordinary calls at a confirmed beep, remove the
  seven-second floor, and let the rep choose **Stay** before the beep. Keep final attempts open for a rep-spoken message.
  Reps handle screening prompts on the open line. The iPhone-specific detection mode is outside this release.
- [ ] Set the outbound ring timeout to 40 seconds by default and make it configurable. Keep it separate from connected call duration.
- [ ] Report all initiated attempts by caller number and receiving carrier with daily totals, weekly and rolling 28-day views.
  Separate network answer, human pickup, usable two-way audio, intended prospect, screening, voicemail, IVR, carrier block,
  failure, conversation and callback. Capture rep-reported audio problems and measure answer-to-bridge p95 against 0.5 seconds.
  A bridge event is a timing proxy. Two-way audio requires rep or media evidence.
- [ ] Warn at 12% short answered calls (six seconds or less) and 15% pre-answer drops. Report ring timeouts separately
  until their treatment is confirmed with Telnyx.
- [ ] Record SIP 603 and 603+ blocks against the original attempt, flag repeats, and pause the number while preserving callbacks.
- [ ] Support a metered Telnyx carrier and line-type lookup with a saved result per lead and endpoint. Enable spend explicitly.
- [ ] Export inventory for Free Caller Registry. Keep manual labels and their source, allow free checks, and show carrier dispute guidance.
- [ ] Hold newly added numbers for at least 24 hours before outbound use. Ramp by completed business days with real use.
  Hold growth while a label or abnormal delivery remains unresolved. Preserve lower owner limits.

  | Business days of real use | Attempts per number per day |
  |---|---|
  | 1 to 2 | 15 |
  | 3 to 5 | 25 |
  | 6 to 10 | 40 |
  | 11 onward | 50 |

  Plan four numbers per rep at 180 attempts per day. A paused number removes capacity from that plan.

- [ ] Run acceptance on SQLite and PostgreSQL and the affected browser journeys.
- [ ] Commit the changes, publish the signed release, update this Mac in the app, and verify the installed workspace in the UI.
- [ ] Remove extra desktop test copies from macOS app discovery while preserving workspace data.

### C. Buy and prove

- [ ] Set limits and obtain approval on exact live quotes. Search again at purchase time for two numbers per metro.
  Prefer San Francisco 415, then San Francisco 628, then Marin 415. In New York prefer 212, 646, 917 or 332, then 929.
- [ ] Check each proposed number and buy the four approved numbers. Submit the inventory once to Free Caller Registry,
  wait at least 24 hours, and record the initial manual label checks.
- [ ] Test unsaved caller IDs on AT&T, T-Mobile and Verizon on iPhone and Android. Include screening prompts,
  voicemail beep and hang-up, the final rep-spoken voicemail, caller display, labels, two-way audio and answer-to-audio time.
- [ ] Prove the workspace test call and both answered and missed callbacks. Confirm A attestation, absence of a carrier notice,
  and more than ten calls in an hour. Exclude all proof calls from production metrics.
- [ ] Resolve failures before ramping.

### D. Ramp

- [ ] Ramp on real prospects for about 11 business days. Watch delivery, labels, callbacks and audio, and resolve any incidents.
  Use actual prospect traffic and callbacks.

### E. Validate and close

- [ ] Run six to eight weeks at normal load and collect at least 5,000 post-ramp attempts.
- [ ] Report overall, weekly and rolling 28-day connect rate, intended-prospect rate, number and carrier breakdowns,
  labels and fixes, screening-to-human conversion, audio losses, conversations per rep-hour and cost per extra connect.
- [ ] Close after live proof passes, the ramp has no unresolved labels, and the report shows a stable rate with no unexplained
  number or carrier decline. Budget using the measured rate.

### F. Optional experiments after the baseline

Change one thing at a time, randomize prospects, and keep each prospect in one arm. Use several numbers per arm, since one against
one only compares two numbers' histories.

- **Branded calling first.** Telnyx: $50, $50 a month, $0.075 per call, in beta, US mobiles on T-Mobile and Verizon only. First Orion
  reportedly covers AT&T too, but its pages could not be opened for this review, so get coverage and price in writing. Fix reputation
  first, because labels can override branding.
- **Then** source-number choice, ring time at 30, 40 and 45 seconds, and a second carrier only if delivery data points there.

Rough attempts per arm (80% power, 5% significance, optimistic because attempts cluster): 10% to 15% needs 700; 15% to 20% needs 910;
10% to 12% needs 3,850; 10% to 11% needs 14,800.

## Incident handling

| Observation | Response |
|---|---|
| One number labeled on one network | Reduce or pause it, keep callbacks working, inspect its traffic, request targeted review |
| Several numbers worse on one network | Check route, attestation and that network's analytics treatment |
| Several numbers worse everywhere | Examine the shared traffic pattern and recipient experience before adding volume |
| Connects fall, labels normal | Check audio, screening handling and carrier failures |
| One poor day on one number | Gather more evidence; do not replace it |

Do not swap numbers as a reputation reset. Hiya can assess a new number from the business's other numbers, and no one-flag-kills-all
rule is documented. A clean launch has no bad history to inherit, but company-wide behavior still counts.

## Not in this plan

Timing, cadence, list buying, pickup-likelihood scoring, email or LinkedIn or SMS before calling, and parallel dialing (owner decision:
manual, one line). Also no fake warmup calls, routine number rotation, shell companies, aged-number promises, paid whitelisting, or
fresh reputation checks before each dial. No audio recording unless the owner asks.

## Risks

- Telnyx policy restricts unsolicited calls that provoke complaints, abandoned calls, dead air, repeated calling and low answer rates,
  and registration is not approval. Telling Telnyx the use case is sensible. Enforcement would stop every number at once.
- Voicemails now ring through the greeting, costing rep time. Measure it. The rep can always hang up sooner.
- Mailbox calls with short greetings can still count as short calls. Without the floor, the 12% warning is the safeguard.
- A low-risk reading is not proof of a clean display everywhere, so real-phone checks stay.
- Display of caller name and branding varies by carrier and device. Nothing guarantees clean labels.

## Sources

- Gong: https://www.gong.io/blog/the-hidden-power-of-cold-calling-insights-from-300m-calls
- Nooks: https://support.nooks.ai/articles/9595602343-strategies-to-increase-your-connect-rates
- Nooks spam protection: https://support.nooks.ai/articles/7296554284-how-nooks-protects-your-calls-from-spam
- Nooks screening data: https://www.nooks.ai/blog-posts/early-data-on-ios-call-screening-what-it-means-for-outbound
- Free Caller Registry: https://freecallerregistry.com/fcr/
- Telnyx short calls: https://support.telnyx.com/en/articles/1130707-what-are-short-duration-calls
- Telnyx abandoned calls: https://support.telnyx.com/en/articles/12805746-surcharge-for-high-abandoned-call-rates
- Telnyx usage policy: https://telnyx.com/acceptable-use-policy
- Telnyx branded calling: https://support.telnyx.com/en/articles/15138152-making-calls-with-branded-calling
- Telnyx answering machine detection: https://developers.telnyx.com/docs/voice/programmable-voice/answering-machine-detection
- Hiya reputation data: https://developer.hiya.com/docs/guides/voice-protection/number-reputation/use-reputation-data
- SIP 603+: https://www.acainternational.org/news/fcc-call-blocking-redress-final-compliance-deadline-is-march-25/
