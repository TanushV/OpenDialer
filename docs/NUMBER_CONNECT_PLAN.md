# Numbers and connect rate plan (draft, November 3, 2026)

Status: approved November 3, 2026. Step 1 shipped as 0.8.3, which also took the voicemail-only hang-up rule from step 2 at the owner's request (detection mode, beep rule, 7-second floor, final attempt stays). Steps 0 and 2 to 5 remain; no real number has been bought or called.

## Goal

- Numbers bought in the app set themselves up.
- Numbers already owned in Telnyx are easy to add and go through the same setup.
- Everything beyond the list and calling habits is handled to best practice.

## Answers to the direct questions

### Recording and transcription

- "Recording off" in route setup only stops Telnyx saving audio of inbound calls (`providers/telnyx.py` `configure_number`).
- The app keeps no audio recording, temporary or otherwise. `control_intent` refuses `record_start`.
- Live transcription is separate and keeps working. On this Mac it streams speech directly (`transcription_source: direct`).
- Raw transcript text expires within five minutes (`privacy.py` `RAW_TTL_SECONDS`). Notes and outcomes stay.
- It is optional under Settings, "Allowed during calls" (Live and Transcription, both on by default).
- Defect: unticking Transcription alone has no effect while Live stays on (`service.py:642-643`). From reading the code, not tested.
- Stale text: "Production transcription stays disabled without this evidence" only applies to the webhook path, not this Mac's direct path.
- Qwen Omni is not used and never shipped. `docs/STATUS.md:52` records it as explored and not adopted. Speech goes to Soniox through Telnyx and becomes text. The live helper reads that text (Cerebras, `openai/gpt-oss-120b`). Nothing analyzes the audio itself.

### No caller name

- Fine. Nothing depends on it. Leave it blank and setup skips it.
- Phones then show the number, sometimes the city, or "Unknown Caller".
- Evidence that a name lifts answer rates is vendor-only.
- Unconfirmed risk: a recycled number might still show a previous owner's name. The real-phone test would reveal it.

### Telnyx limit

- 10 calls an hour and 100 a day are Paid-tier Voice API limits. Paid also allows 5 calls at once.
- Verified needs an identity check (KYC) and Telnyx's "AI agent evaluation".
- Telnyx calls Verified the tier required for high-volume production traffic. Its page lists no call caps, and the automated-call notice stops there.
- Telnyx does not state in words that the hourly cap is removed, so ask support to confirm.
- An older Telnyx article says Level 2 allows 10 concurrent calls, with more on request to support@telnyx.com.
- There is no workaround worth building. Moving to a second carrier is a real project and only makes sense if Telnyx refuses.

### Call screening and voicemail

- Today: any `machine` result on a non-final attempt hangs up immediately (`telephony/workflows.py:135-147`).
- Standard detection decides within 3.5 seconds by default, so these calls probably last about 4 seconds. Not measured.
- Telnyx's docs indicate an iPhone screening prompt first reads as `machine`. So screened calls are likely hung up today. Not tested.
- Nooks measured 2.5% of calls reaching a voice assistant, so this is a small but real share.

### New finding: instant voicemail hang-ups break a Telnyx rule

- Telnyx counts outbound calls of 6 seconds or less as Short Duration Calls.
- If they exceed 15% of connected calls in a calendar month, a surcharge applies to all of them. Its terms list $0.01 per call.
- Its terms also allow it to move or block that traffic, and it says it does not support use cases that require such calls.
- Voicemail hang-ups at about 4 seconds would all count. I expect voicemails to be well over 15% of connected calls. [ASSUMPTION]
- Separately, if more than 20% of calls are dropped by us before answer, there is a $0.005 per call surcharge. Calls to disconnected numbers count too.

## What top teams do beyond lists and habits

| Practice | Nooks | Others | Us today | Plan |
|---|---|---|---|---|
| Register business with First Orion, Hiya and TNS | Yes, with legal name, website, industry, use case, EIN | Orum and Salesfinity claim carrier registration | Link only | Step 3 |
| Wait after registering | At least 24 hours for its pool numbers | Not stated | None | Step 3 |
| Spam test each number | About daily, plus every 60 dials | Orum and Salesfinity monitor | None | Step 3 |
| Spread volume | Switches after 60 dials a day, 10 numbers per rep | Salesfinity rotates every 5 calls, others 70 to 250 a day | 40 a day max, least-used first | 60 a day cap, stable pool |
| Drop flagged numbers | Auto on its pool, manual for your own | Similar claims | None | Step 3 |
| Watch answer rate and short calls | Yes | Yes | Built (`assess_number_health`), hidden in review | Step 3 |
| Remediate labels | Through vendor partners | Yes | None | Step 3 ($1 per number) |
| Pick number by data | Smart Presence | Orum Boost Connect | Same state, area code as tie-break | Metro first now, data later |
| Detect screening separately | Yes, with its own queue and talk track | Growing | No | Step 2 |
| Callback handling | Rings app or mobile, greeting, voicemail, missed list | | App, message and task; no fallback saved | Step 1 |
| Branded calling | Not named on its spam page | Sold separately | None | Later test |

Nooks data on assistant-answered calls: real context got through 11.6% of the time, concise keywords 8.7%, and evasive commands failed 99%.

Disagreement: Hiya advises against rotating, and scores "Maturity". Nooks, Orum and Salesfinity all rotate. This plan keeps a stable pool, gives each lead one number, and shares load. It does not rotate every few calls or replace numbers every 30 days.

All of the above is vendor-reported. No independent study settles it.

## Order of work

### Step 0: you, now (runs alongside the builds)

1. Complete Telnyx Verified in Account > Levels. I can draft a support message that describes human reps calling businesses one at a time and asks them to confirm the hourly and daily caps are gone.
2. Turn on auto-recharge and add funds. The balance is $15.
3. Gather: legal name, trade name, EIN, website, address, contact person, industry, employee count, call purpose and a callback phone number.
4. Decide the caller name, or none.
5. Ask a lawyer two things:
   - Is live AI transcription on calls into California acceptable, or should reps disclose it or turn it off for California? (*Ambriz v. Google* let a similar claim past dismissal in 2025.)
   - Do cell phones in the list need do-not-call screening?
6. Try Apple Business and see whether Business Caller ID is offered for your numbers. Apple publishes a caller ID API, but I could not confirm availability, eligibility or price.
7. Make your business easy to find when someone searches a number: same legal and trade name everywhere, and your calling numbers on your website contact page. Low cost, weak evidence.

### Step 1: release 0.8.3, easy numbers (no new paid services)

Tests first, on SQLite and PostgreSQL. Remove code where possible.

1. **Search.** Show each number's city and allow search by city, so SF means San Francisco-located 628s. `search_numbers`, `/v1/admin/numbers/search`, `NumberPurchasing.tsx`. The price recheck must repeat the same search.
2. **Buy.** Approving a quote also confirms the route, recording off and active status. No Set up route click. `purchase_number`, `reconcile_number_order`.
3. **Add owned numbers.** List your Telnyx numbers with an Add button, running the same setup. Numbers on another Telnyx app need a confirmation, because adding moves them. Remove the "Add owned number" box, which can create an entry that can't be set up. `sync_numbers`, `_merge_inventory`, `Operations.tsx`.
4. **Test call.** One real test per workspace, not per number, because every number shares one Telnyx app and call path. No 30-day expiry. Telnyx's route read-back qualifies each number. Replace the relay-list filter with the confirmed connection. `number_ready`, `number_readiness_in`, `_start_warmup`, `queue.py:250`, `service.py:775` and `905`.
5. **Warmup.** Starts at setup. Starting at 15 a day, rising with clean real use to a 60 a day maximum. Show the real limit, such as "15 today, 60 max". `WARMUP`, `daily_capacity_per_number`, `adaptive_daily_limit`. [Judgment: 5 was our own guess, and Nooks uses 60 a day.]
6. **Number choice.** Same metro first, then least used, treating overlays as the same (415 and 628; 212, 646, 332, 917, 718, 347 and 929). Each lead keeps its number. `select_number`, `geography.py`.
7. **Caller name.** Optional, one saved value, applied automatically if set.
8. **Callbacks.** Ask for the callback phone during setup so returned calls reach a person. Replace the generic "We cannot take your call" message with a short greeting naming the business. Callers you never dialed no longer ring reps (the workspace test call is the exception). `telephony/callbacks.py`. No in-app voicemail, because the app keeps no audio; the callback phone's voicemail covers it.
9. **Clear errors.** Say when a number is the blocker, not "No one to call right now". Name states with no number. `client.ts`, queue reasons.
10. **Settings.** One "Live transcription and help" switch, stale text removed.
11. **Spend cap.** Set the Telnyx daily spend limit on the voice profile, with a saved amount.
12. Ship, publish and update this Mac through the signed updater.

### Step 2: release 0.8.4, voicemail, screening and short calls (before any real calling)

1. Use `premium_ios_call_screening_detection` ($0.0065 instead of $0.002 per call). `service.py:827`.
2. New rule (`workflows.py:135-147`, `reducer.py`):
   - A `machine` result never hangs up by itself.
   - A beep means voicemail: hang up, unless it's the final attempt or the rep pressed Stay.
   - Never hang up before 7 seconds from answer.
   - Screening prompt or tone means stay. Show "Say your name and reason" with the script's line.
   - Machine with no beep or prompt: the rep decides, with an automatic end at about 30 seconds.
   - A human result always cancels any pending hang-up.
3. Add a Stay button on any voicemail. `DialerPage.tsx`.
4. Handle the new Telnyx results, including `prompt_ended`. Android screeners have no special signal, but the beep rule should cover them. Not tested.
5. Script: a short screening line using real context, name, company and specific reason, 12 to 18 words. Update the preparation prompt.
6. Show on Numbers: short-call share (warn at 12%), pre-answer drop share (warn at 15%) and screening rate.
7. **Carrier block signal.** Read `sip_hangup_cause` on hangups (`workflows.py:124-127` only reads `hangup_cause`). SIP 603+ has been the required notice of analytics-based blocking since March 25, 2026. Count it per number, don't use up the lead's attempt, pause the number on repeats and alert. It covers blocks only, not labels.

Cost: about $0.0045 more per call, plus a few seconds of rep time per voicemail. Longer voicemail calls should also look better to the spam-label firms.

### Step 3: number health, also in 0.8.4

**If Number Reputation is on (my recommendation once Verified):**

1. One business form in the app. It creates the Telnyx Enterprise, renders the authorization letter for you to sign, uploads it, and records your acceptance of the terms.
2. Adding a number registers it and reads its spam risk. It shows "Registering, ready after [time]" for at least 24 hours.
3. High risk: won't dial. Offer a replacement, which you approve.
4. Check on business days, about $2.20 per number a month. Medium risk: lower the limit. High risk: pause and offer re-evaluation ($1).
5. Show spam risk, answer rate, short-call rate and calls today per number. Reuse `external_reputation` and `assess_number_health`.

Cost: $100 a month plus checks. Update the app text that says this subscription is blocked.

**If not:** the app exports your numbers for the free registry form, links Hiya's free per-number report, and you enter results using the existing reputation import.

**Either way:**

1. **Per-carrier results.** Labels are separate per carrier (reported: AT&T uses Hiya, T-Mobile uses First Orion, Verizon uses TNS). Look up each lead's carrier and line type once through Telnyx Number Lookup, about $0.25 per 100 leads at listed prices. Show answer rate per number per carrier.
2. **Early warning.** Alert when a number answers far worse than the others, or on one carrier. Run a fresh spam check on that alert.
3. **Mobile first.** Use the line type in lead ranking. Nooks reports 45% higher connect rates on mobiles.
4. **Disputes.** Link the free AT&T, T-Mobile and Verizon review forms and Hiya from a flagged number, in addition to the Telnyx re-evaluation.

### Step 4: buy and prove it on real numbers

1. Set limits: 2 numbers, $2 initial, $2 monthly.
2. Optionally run each candidate through CallerAPI's free test first.
3. Buy one San Francisco 628 and one New York City 929. $4 today, $2 a month.
4. Register, then wait 24 hours.
5. Allow the microphone. Go ready.
6. Real calls to AT&T, T-Mobile and Verizon phones, and an iPhone with screening on. Check:
   - any label or caller name shown
   - voicemail behavior and call length over 6 seconds
   - screening flow
   - one callback, answered and missed
   - time from answer to hearing the rep
   - the "A" rating in the Telnyx report
   - that no Telnyx notice plays
7. Record evidence in `docs/evidence/`.

### Step 5: call and tune

- Weekly: answer rate per number, spam risk, short-call share, pre-answer drops.
- Buy more numbers when the capacity plan says so: calls per day divided by 60, and at least one per rep calling at once in each state.
- After a few hundred calls, test Telnyx Branded Calling on one number: $50, $50 a month, $0.075 per call, still in beta.
- At steady volume, keep one registered spare number per metro so a flagged number doesn't stop calling.
- With enough calls, test data-driven number choice using the existing comparison framework: reuse a number that has connected with that prospect, and try another in the same metro after two no-connects. This conflicts with one number per lead, so it stays an experiment.
- If labels are hard to catch, add daily real-device testing from a vendor such as OptimalDial. Price unknown.
- If Telnyx delivery itself looks poor, or Telnyx declines Verified, evaluate a second carrier.

## Deliberately not doing

- Parallel dialing (your decision: manual Space/Next).
- Rotating every few calls, or replacing numbers every 30 days.
- Fake warmup calls.
- Separate companies to split reputation.
- A paid pre-purchase spam API. A bad number costs $2 to find out.
- Audio recording, unless you ask for it.
- Telnyx Repeat Call Guard: it only applies to SIP connections, and the app already spaces retries.

## Outside this plan

- Email, LinkedIn or SMS before a call. Salesfinity claims 20% to 40% higher connect rates, with no source given. The app has no such channels, and SMS needs 10DLC, which the Paid tier can't use. Reps can do this outside the app. Your call.
- Buying better lists or mobile numbers. This is likely the biggest remaining lever, but it is a data purchase, not app work.
- What only large vendors have: private analytics partnerships for faster fixes, and connect data from millions of calls.

## Open risks

- Nothing is proven on a real number yet.
- Telnyx may not confirm the cap removal, or may decline Verified. Then consider a second carrier.
- No one can guarantee clean labels. Hiya scores businesses across their numbers, so behavior on one number can affect the others.
- Vendor claims throughout are not independently verified.

## Sources

- Nooks: https://support.nooks.ai/articles/7296554284-how-nooks-protects-your-calls-from-spam
- Nooks: https://support.nooks.ai/articles/8714570394-smart-presence
- Nooks: https://support.nooks.ai/articles/3119124430-understanding-callbacks-within-nooks
- Nooks: https://www.nooks.ai/blog-posts/how-nooks-keeps-your-numbers-spam-free
- Nooks: https://www.nooks.ai/blog-posts/mastering-connect-rates
- Nooks: https://www.nooks.ai/blog-posts/early-data-on-ios-call-screening-what-it-means-for-outbound
- Orum: https://support.orum.com/en-US/orum/article/XKSKR_od-orum-101-quickstart-guide-for-sales-reps
- Salesfinity: https://support.salesfinity.ai/articles/9577368303-what-is-smartguard-in-salesfinity
- Telnyx short calls: https://support.telnyx.com/en/articles/1130707-what-are-short-duration-calls
- Telnyx terms: https://telnyx.com/terms-and-conditions-of-service
- Telnyx abandoned calls: https://support.telnyx.com/en/articles/12805746-surcharge-for-high-abandoned-call-rates
- Telnyx repeat guard: https://support.telnyx.com/en/articles/12580667-configure-repeat-call-guard-on-outbound-voice-profiles-beta
- Telnyx tiers: https://developers.telnyx.com/docs/account-setup/account-upgrade
- Telnyx Paid: https://developers.telnyx.com/docs/account-setup/levels-and-capabilities/paid
- Telnyx Verified: https://developers.telnyx.com/docs/account-setup/levels-and-capabilities/verified
- Telnyx concurrency: https://support.telnyx.com/en/articles/1130717-limits-on-concurrent-outbound-calls
- Telnyx machine detection: https://developers.telnyx.com/docs/voice/programmable-voice/answering-machine-detection
- Telnyx Number Reputation: https://developers.telnyx.com/docs/number-reputation/overview
- Telnyx Branded Calling: https://developers.telnyx.com/docs/branded-calling/pricing
- Hiya labels: https://blog.hiya.com/how-carriers-label-spam-calls-and-how-businesses-can-support-a-healthy-call-reputation
- Hiya checks: https://blog.hiya.com/how-to-check-phone-numbers-for-spam-labels
- Hiya scoring: https://www.hiya.com/products/enterprise-caller-scoring
- Twilio on iOS screening: https://www.twilio.com/en-us/blog/developers/tutorials/product/detect-ios-call-screening-amd-transcriptions
- Screening guide: https://www.revenue.io/blog/getting-past-ai-phone-screeners-a-sales-dialer-guide
- CallerAPI test: https://callerapi.com/caller-id-test
- Ambriz v. Google: https://natlawreview.com/article/extension-vs-capability-google-learns-difference-hard-way
- SIP 603+ deadline: https://www.acainternational.org/news/fcc-call-blocking-redress-final-compliance-deadline-is-march-25/
- SIP 603+ order: https://docs.fcc.gov/public/attachments/FCC-25-15A1_Rcd.pdf
- Telnyx Number Lookup pricing: https://telnyx.com/pricing/number-lookup
- Per-carrier labels: https://www.twinsai.com/resources/caller-id-reputation
- Real-device monitoring: https://optimaldial.com/spam-monitoring
- Apple caller ID API: https://business.apple.com/docs/api/v1/caller_id/create
- Apple announcement: https://www.apple.com/newsroom/2024/10/apple-expands-tools-to-help-businesses-connect-with-customers/
- Pre-call touches claim: https://salesfinity.ai/blog/how-to-increase-cold-call-connect-rates-in-2026-a-data-driven-guide
