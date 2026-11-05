# Numbers and connect rate plan

Updated November 4, 2026. Both numbers are active and ready. A live rep answer passed the shared callback test, and Telnyx confirmed outbound delivery from each number. The Dialer also completed a live call between the owned numbers. Two-way speech between people still needs verification.

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
- One workspace test call with no expiry. Callers on no lead hear the fallback greeting.
- City search with each number's city, tried one area code at a time. Same metro first, one number per lead.
- Telnyx iPhone-aware detection. Only a voicemail beep hangs up an ordinary attempt. The final attempt stays on the line.
- The rep is bridged on answer, so detection does not hold up live audio.
- Clear refusals, one transcription switch, optional caller name, $25 daily spend cap (set on the real account).

Telnyx account: the owner reports it is now Verified. The hourly cap and automated notice have yet to be checked on live outbound calls.

## Close-out order

### A. Owner setup

- [x] Confirm Telnyx shows Verified in the console. Check real calling limits and announcements during live proof.
- [x] Decide number health scope. Use Free Caller Registry, manual reports and observed delivery. Skip Telnyx Number Reputation.
- [ ] Fund the calling balance and decide auto-recharge.
- [ ] Set the callback destination and optional caller name. Review the applicable transcription and do-not-call rules.

### B. Releases 0.8.5 and 0.8.6

- [x] Request Telnyx voicemail AMD with the rep bridged on answer. End ordinary calls at a confirmed beep, remove the
  seven-second floor, and let the rep choose **Stay** before the beep. Keep final attempts open for a rep-spoken message.
  Reps handle screening prompts on the open line. The iPhone-specific detection mode is outside this release.
- [x] Set the outbound ring timeout to 40 seconds by default and make it configurable. Keep it separate from connected call duration.
- [x] Report all initiated attempts by caller number and receiving carrier with daily totals, weekly and rolling 28-day views.
  Separate network answer, human pickup, usable two-way audio, intended prospect, screening, voicemail, IVR, carrier block,
  failure, conversation and callback. Capture rep-reported audio problems and measure answer-to-bridge p95 against 0.5 seconds.
  A bridge event is a timing proxy. Two-way audio requires rep or media evidence.
- [x] Warn at 12% short answered calls (six seconds or less) and 15% pre-answer drops. Report ring timeouts
  separately from estimated drops. Confirm the billing treatment of ring timeouts with Telnyx during live proof.
- [x] Record SIP 603 and 603+ blocks against the original attempt, flag repeats, and pause the number while preserving callbacks.
- [x] Support a metered Telnyx carrier and line-type lookup with a saved result per lead and endpoint. Enable spend explicitly.
- [x] Export inventory for Free Caller Registry. Keep manual labels and their source, allow free checks, and show carrier dispute guidance.
- [x] Ramp by completed business days with real use. The owner removed OpenDialer's 24-hour registration timer in 0.8.10.
  Use Pacific time for weekdays. An open label blocks outbound until explicitly cleared. A number under delivery review
  falls back to a maximum of 15 attempts per day. Preserve lower owner limits.

  | Business days of real use | Attempts per number per day |
  |---|---|
  | 1 to 2 | 15 |
  | 3 to 5 | 25 |
  | 6 to 10 | 40 |
  | 11 onward | 50 |

  Plan four numbers per rep at 180 attempts per day. A paused number removes capacity from that plan.

- [x] Verify native registry CSV export on this Mac. The 0.8.5 WebKit download path stalled in the signed app.
- [x] Run acceptance on SQLite and PostgreSQL and the affected browser journeys.
- [x] Commit the changes and publish signed Mac and Windows builds.
- [x] Install 0.8.6 through this Mac’s updater and verify the installed workspace in the UI.
- [x] Remove extra desktop test copies from macOS app discovery while preserving workspace data.

### C. Buy and prove

- [ ] Set limits and obtain approval on exact live quotes. Search again at purchase time for two numbers per metro.
  Prefer San Francisco 415, then San Francisco 628, then Marin 415. In New York prefer 212, 646, 917 or 332, then 929.
- [ ] Check each proposed number and buy the four approved numbers. Submit the inventory once to Free Caller Registry
  and record the initial manual label checks.
- [ ] Test unsaved caller IDs on AT&T, T-Mobile and Verizon on iPhone and Android. Include screening prompts,
  voicemail beep and hang-up, the final rep-spoken voicemail, caller display, labels, two-way audio and answer-to-audio time.
- [ ] Prove the workspace test call and both answered and missed callbacks. Confirm A attestation, absence of a carrier notice,
  and more than ten calls in an hour. Exclude all proof calls from production metrics.
- [x] Install the signed inbound fixes on the Mac and confirm the existing workspace and number reopen.
- [x] Verify the greeting, hangup and callback task on a real inbound call when no rep is ready, using signed carrier events.
- [x] Confirm both purchased numbers are active with recording disabled, and prove inbound routing to the callback queue on both.
- [x] Fix the order list that kept showing Pending after a successful inventory sync. Cover the transition on SQLite and PostgreSQL.
- [x] Verify the signed order-status fix in the installed Mac app.
- [x] With **Go ready** complete before dialing, answer a live test call in OpenDialer.
- [ ] Confirm two-way audio on a live call with a separate phone.
- [x] Verify carrier outbound delivery from each purchased number using an authorized test destination.
- [x] Start and complete a live test call from the Dialer without dialing a campaign lead.
- [x] Verify the new callback greeting plays and ends on a live Telnyx call.
- [ ] Install the signed 0.8.12 update with the new greeting on the Mac.
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

## Verification checkpoint

- The 0.8.6 runtime is commit `80ec1e1`. [CI](https://github.com/TanushV/OpenDialer/actions/runs/41815242317)
  passed 820 SQLite tests, 821 PostgreSQL tests and 179 browser journeys. Mac and Windows packaging passed.
  The Mac archive checksum and deep strict code signature verified. [Signed release](https://github.com/TanushV/OpenDialer/releases/tag/v0.8.6)
- This Mac installed 0.8.5 through the signed updater. The report loaded and outbound ringing defaulted to 40 seconds,
  but CSV export stalled in WebKit’s download path. The Mac then installed 0.8.6 through the signed updater and passed
  deep strict signature verification. The existing Agite workspace reopened with three leads and Telnyx still connected.
  In Numbers, native export saved a valid CSV with the `phone_number` header and zero data rows, matching the empty
  inventory. The rolling 28-day report opened after export, confirming the app stayed responsive.
- Two extra OpenDialer Journeys bundles were moved to the Trash. macOS app discovery lists the single
  `/Applications/OpenDialer.app` copy.
- The console's Verified status is owner-confirmed. Carrier lookup is explicitly approved per endpoint in the lead packet.
  Report time buckets use Pacific time. Bridge latency is a proxy and the billing warnings are estimates.
  Additional purchases, live outbound calls, ring-timeout billing confirmation and measured lift remain open.
- The first owned 628 number is active with its route confirmed. Its first inbound test arrived before the rep's audio
  session was ready. OpenDialer queued a callback, but did not send the greeting before the call timed out. The incident
  also exposed that **Go ready** uses the workspace SIP login for owners and admins, while 0.8.6 inbound routing only
  uses per-user SIP. Signed 0.8.7 aligned routing with readiness and deferred the carrier status lookup for fresh calls.
  The signed Mac update preserved the workspace. A real call through the Mac's Phone app reached the relay and opened a
  callback task, but the answer and greeting still timed out. A regression using an advancing clock reproduced the cause:
  the control dispatcher compared two separate clock readings when no send time was scheduled. The 0.8.8 fix uses one
  timestamp per dispatch. The affected SQLite and PostgreSQL acceptance suites pass, with 46 cases per engine.
- [0.8.8](https://github.com/TanushV/OpenDialer/releases/tag/v0.8.8) was built from `4b228d9`. All six
  [CI jobs](https://github.com/TanushV/OpenDialer/actions/runs/41838308724) passed. The Mac and Windows builds signed,
  the Mac archive checksum matched, and this Mac installed through the signed updater and reopened the existing workspace.
  The 5:42 a.m. Pacific test call produced signed Telnyx answer, speech start, speech end and hangup events. OpenDialer
  kept an open callback task and sent the hangup after the greeting.
- After the owner allowed microphone access, **Go ready** showed the rep session ready. A live transfer produced
  `call.bridged` while the rep leg was still ringing. The destination hung up with SIP 487 and no `call.answered`.
  OpenDialer 0.8.8 had counted the early bridge as proof. The 0.8.9 patch disables early media on transfers and
  requires both a signed answer and bridge on the same leg. It requires a fresh test for old live proofs without
  answer evidence. OpenDialer 0.8.9 also imposed its own 24-hour registration hold on the first number.
- [0.8.9](https://github.com/TanushV/OpenDialer/releases/tag/v0.8.9) was built from `893e3c6` and passed all six
  [CI jobs](https://github.com/TanushV/OpenDialer/actions/runs/41903398877). This Mac installed it through the signed
  updater after the original idle rep audio leg was verified and ended at Telnyx. The existing workspace has three
  leads, one number and zero outbound attempts. After the Mac was unlocked, **Go ready** succeeded. A call from the
  Mac's Phone app rang in OpenDialer. Telnyx recorded a transfer timeout and no destination answer. OpenDialer correctly
  withheld the workspace pass, played the fallback greeting and kept the callback task open. The live answer and two-way
  audio retest from a separate phone remain open.
- The owner asked to remove the app's registration hold. Version 0.8.10 ignores previously saved timers and stops setting
  new ones. Carrier ownership, confirmed routing, the answered workspace test, number health and the warmup ramp still
  determine readiness. The failed inbound answer reached the WebRTC remote offer but produced no local answer. At that
  point the SDK tries to open another microphone stream. The patch gives it a clone of the rep's active stream instead.
- [0.8.10](https://github.com/TanushV/OpenDialer/releases/tag/v0.8.10) was built from `324c0ad` and passed all six
  [CI jobs](https://github.com/TanushV/OpenDialer/actions/runs/41913771224). The Mac installed it through the signed
  updater and the running local authority reported 0.8.10. macOS requested a fresh microphone grant after the update
  changed the bundle signature. The live answer retest awaits that grant.
- The saved Telnyx key confirmed both order and line items succeeded. Both numbers were active on the workspace voice
  route with recording disabled after the app synced. A 1:42 p.m. Pacific call to the second number reached the signed
  inbound route. The answer, greeting and hangup commands completed and OpenDialer kept an open callback task.
  The order list still displayed Pending from the original purchase receipt. The 0.8.11 fix displays current carrier
  inventory status after sync and keeps **Check order status** available until the route setup is confirmed. The raw
  purchase receipt stays available in the backend. The affected acceptance suite passed 25 cases on each database
  and the five number-purchase browser journeys passed.
- The first 0.8.11 draft's full browser run exposed a simulation bug. When the voicemail beep ended the call
  before a rep hung up, the simulator skipped finalization and the optional next-call timer did not start.
  A regression reproduces that sequence through the HTTP control runner. The fix queues finalization on the
  simulated hangup receipt. Live Telnyx finalization continues to use the carrier's hangup webhook.
- [0.8.11](https://github.com/TanushV/OpenDialer/releases/tag/v0.8.11) was built from `449f263` and published
  after both signed builds passed and the Mac archive matched the release checksum and passed deep signature
  verification. Both full database suites passed. The browser run passed the number and voicemail journeys
  and reported 178 of 179 passing overall. The remaining sign-in test let a background request renew the session
  when its mock was removed before the click. With the mock in place until the click, all 14 rep-screen journeys
  passed locally, and [all six CI jobs](https://github.com/TanushV/OpenDialer/actions/runs/41924282332) passed on
  the follow-up commit, including the full browser suite.
- After microphone access was granted to 0.8.10, the 3:22 p.m. Pacific inbound test on the first number rang and
  connected in OpenDialer after **Accept return call**. Signed answer and bridge events on the rep leg passed the
  shared workspace test. The return call ended normally. The idle rep leg remained connected after the app paused
  and blocked the updater. Telnyx confirmed it was still live, accepted a hangup for that idle leg, and the updater proceeded.
- This Mac installed 0.8.11 through the signed updater. The installed bundle passed deep strict verification, the
  local service reported 0.8.11, and the existing workspace reopened. Both order receipts show **Ordered · Active**
  and **Set up and in your numbers**, Numbers shows **2 Ready for outbound use**, and the Settings readiness check
  shows **Ready to call**. After macOS granted microphone access for this build, **Go ready** reached **Audio ready**.
  **Pause dialing** then disconnected the idle session normally.
- The two short Telnyx tests to the owner-authorized destination produced signed initiated, answered and normal
  hangup events from each owned caller number. These calls used the carrier API and produced no campaign attempts.
  An answer event proves delivery to an answering endpoint.
- At 3:38 p.m. Pacific, the installed 0.8.11 Dialer called from the number ending in 8854 to the owned number
  ending in 8693. Telnyx signed the initiated, answered, bridged, machine-detection and hangup events. The bridge
  followed answer by 540 ms in carrier timestamps. The receiving number played the fallback greeting. The saved
  outcome marks this as a delivery test, the test contact is held, and the test callback tasks are closed.
- The shorter 0.8.12 greeting uses Telnyx's Kokoro voice. The Mac's workspace briefly ran the patched authority
  for an owned-line test. Telnyx confirmed speech start, speech end and hangup, and the command receipt recorded
  the Kokoro voice. The affected suites passed 50 cases on SQLite and 50 on PostgreSQL. The owner declined
  Apple Developer ID signing, so new preview builds may still trigger a fresh macOS microphone grant.
- Telnyx's stock greeting is for its SIP voicemail mailbox. On the existing Voice API route, Telnyx speaks
  “You've reached Agite. We're unavailable right now. We'll return your call.” The greeting uses the
  workspace's business name and OpenDialer creates a callback task.

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
