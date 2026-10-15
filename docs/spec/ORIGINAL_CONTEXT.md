# Full implementation specification

**Build a Tauri desktop app backed by one small, authoritative server. Use OpenAI centrally for research, scripts, and post-call analysis; Groq for fast live guidance; and Telnyx for calls and transcription.**

The complete specification is here:

**:chatgpt-content-reference{index="26"}[Download the Markdown implementation plan](sandbox:/mnt/data/telnyx_tauri_adaptive_dialer_build_spec.md)**

It includes the architecture, database entities, API contracts, example payloads, job-queue SQL, repository structure, deployment configuration, call state machine, ML design, coaching pipeline, cost calculations, and acceptance tests. It is a specification, not an already implemented or tested application.

Below are the principal decisions and how the system fits together.

## 1. The architecture

### One desktop application, one modest backend

| Component | Implementation | Responsibility |
|---|---|---|
| **Desktop** | Tauri v2, React, TypeScript, narrowly scoped Rust functions | Calling interface, headset audio, lead packets, scripts, live guidance, notes, and local cache. |
| **Shared backend** | Python, FastAPI, PostgreSQL | Calling permissions, number pools, lead reservations, call control, callbacks, research, scripts, and shared learning. |
| **Background workers** | Separate Python processes using a durable PostgreSQL queue | Research batches, script generation, post-call analysis, model training, and maintenance. |
| **Telephony and speech** | Telnyx WebRTC, Voice API, transcription | Telephone connectivity, audio, call-leg control, speech recognition, and permitted voicemail playback. |
| **Language models** | OpenAI and Groq adapters | OpenAI for preparation and deeper analysis; Groq for short, time-sensitive coaching responses. |

**Do not run large language models on the cheap VM.** It runs the orchestration and small statistical models, while hosted providers perform the expensive inference.

The intended audio path is:

```text
Rep headset / Tauri ↔ Telnyx ↔ Prospect

                           Transcript events
                                  ↓
                            Shared backend
                                  ↓
                           Groq live guidance
                                  ↓
                             Tauri display
```

Your server does not relay the conversation’s audio. Telnyx documents the browser-agent pattern with individual credentials and backend-issued tokens. [Telnyx](https://developers.telnyx.com/docs/voice/webrtc/use-cases/contact-center)

That is how we preserve responsiveness without giving every desktop the master carrier credentials.

### Why retain a shared backend?

Because numbers, leads, opt-outs, and callbacks are shared resources.

The backend must answer, consistently: **Is this lead eligible now? Has another rep reserved it? Which number should appear? Did the prospect just opt out? Who owns the callback?**

Local caches improve responsiveness. They must not become competing versions of the truth.

---

## 2. Hosting: free is possible, but not the entire operating budget

### Free pilot: Oracle Always Free

Oracle’s current documentation describes an A1 allowance equivalent to **2 OCPUs and 12 GB of memory**, plus **200 GB of combined block storage**. Capacity availability and idle-instance reclamation are material limitations. Do not plan from older tutorials advertising different allowances. [Oracle Docs](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)

That is a plausible development and pilot host for this architecture. It is not high availability.

### Paid reference: $12–$24 per month

DigitalOcean currently lists **1 vCPU/2 GiB for $12/month** and **2 vCPU/4 GiB for $24/month**, before extras such as backups. I would use the latter as the initial paid reference and validate capacity with the actual workload. [DigitalOcean](https://www.digitalocean.com/pricing/droplets)

The server runs:

- The API, database, and high-priority call-control workers.
- Bounded research, script, and post-call workers.
- Off-peak statistical-model training.

Do not put unrestricted browser automation or heavy model training beside the call controller. Saving twelve dollars is not useful when a research job freezes everyone’s dialer.

**Failure policy:** existing calls should continue where the carrier topology permits; coaching falls back to cached material; new pooled outbound calls pause when authorization cannot be checked.

---

## 3. Centralized research and script generation

Your proposed research-to-writer pipeline is sensible. I would implement it as **bounded, versioned jobs**, rather than an unrestricted group of agents.

### Before the rep’s shift

```text
Upcoming eligible leads
    ↓
Group by company/domain
    ↓
Reuse fresh research where available
    ↓
Discover and retrieve permitted sources
    ↓
Extract evidence-linked facts
    ↓
Write or modify scripts
    ↓
Validate identity, claims, and output structure
    ↓
Publish versioned lead packets
    ↓
Prefetch into the rep’s Tauri app
```

Start with work units of approximately **20 leads**, grouped by company where possible. That is a proposed operational setting, not a provider limit.

Each lead retains its own ID, evidence, and output. Shared company research avoids paying repeatedly to discover the same company’s services page.

### Research output

The research agent should return structured facts with their source, retrieval time, entity match, and uncertainty.

For an SEO-related offering, useful facts include services, locations, visible website content, and observable contact paths. **Search results do not justify inventing traffic, rankings, advertising spend, or revenue.**

Use OpenAI’s web-search tool when appropriate, avoiding a separate search subscription. Searches are still metered; “included in the API” does not mean free. [OpenAI Developers](https://developers.openai.com/api/docs/guides/tools-web-search)

### Script output

Each packet should contain a brief opener, discovery questions, approved objection responses, a truthful screening introduction, and relevant evidence.

The modifier updates a script when verified facts or approved playbooks change. It should not rewrite the rep’s entire screen during a conversation.

**Keep a small set of approved variants.** Generating completely different language for every call makes causal evaluation harder.

### Which “ChatGPT endpoint”?

For a shared production backend, my default is **OpenAI project/service-account credentials and the Responses API**.

There is also now an official **ChatGPT-plan usage route for eligible applications**, including a documented self-hosted VM flow. It has eligibility and preview limitations, so I included it as an optional adapter rather than assuming one consumer account is an unlimited team inference service. Do not use undocumented browser-session endpoints or shared cookies. [OpenAI Developers](https://developers.openai.com/siwc/token-sharing-open-source)

For nonurgent work, OpenAI Batch offers **50% lower pricing with a completion window of up to 24 hours**. Use it for advance preparation, not scripts needed immediately before a shift. [OpenAI Developers](https://developers.openai.com/api/docs/guides/batch)

---

## 4. Number validation, DNC, and caller identity

### “Does this number exist?” needs several answers

Keep these separate:

**Valid format → plausible assignment → historical reachability → correct ownership → permission to call.**

A correctly formatted number is not necessarily assigned, reachable, or owned by the intended person. Google explicitly warns against using `libphonenumber` as proof of those properties. Telnyx lookup adds carrier information, not ownership or DNC certification. [GitHub](https://github.com/google/libphonenumber/blob/master/FAQ.md)

The implementation should combine local normalization, deduplication, optional cached carrier lookup, and first-party outcomes.

A confirmed wrong endpoint is suppressed. **The lead remains available for correction or re-enrichment.** Keeping expensive leads does not require repeatedly calling the wrong person.

### DNC is an authoritative dataset check

The system needs seller-specific authorization, current national and applicable state data, internal opt-outs, and a permission check immediately before dialing.

FTC guidance calls for National Registry synchronization at least every **31 days**. I would import authorized updates more frequently where supported and fail closed when required coverage is missing or stale. [Federal Trade Commission](https://www.ftc.gov/business-guidance/resources/complying-telemarketing-sales-rule)

The current federal fee schedule provides the **first five area codes free**, then **$85 per additional area code annually**, subject to the national cap. Fees cannot simply be pooled across unrelated sellers. [eCFR](https://www.ecfr.gov/current/title-16/chapter-I/subchapter-C/part-310/section-310.8)

For example, 30 area codes would be:

**25 paid area codes × $85 = $2,125/year, approximately $177/month.**

This is one reason “the technology is nearly free” does not mean the operation is nearly free.

Keep registry data out of the ML feature set. It is used to enforce eligibility, not to enrich or rank prospects.

### Shared regional number pools

Use seller-specific pools with this allocation order:

**Existing healthy prospect-number assignment → appropriate healthy regional number → healthy general business number.**

Each rep receives a temporary working set, while the backend coordinates shared inventory. Preserve the number shown to a prospect across attempts when healthy.

Warmup means gradual, legitimate use with callback testing and monitoring. It does not mean fake conversations or number replacement to dodge enforcement. Registration helps establish identity but does not guarantee removal of spam labels. [Free Caller Registry](https://www.freecallerregistry.com/fcr/)

---

## 5. Adaptive ML: improve treatment without abandoning leads

### Start with statistical learning, not neural complexity

Published work has already examined individualized outbound scheduling and improving right-party contacts. The concept is established; the lift for your campaign still requires measurement. [Wiley Online Library](https://onlinelibrary.wiley.com/doi/abs/10.1111/j.1937-5956.2010.01156.x)

My progression is:

**Smoothed segment/time estimates or pooled logistic regression → calibrated gradient boosting → advanced challenger only when it wins.**

Tabular foundation models such as TabPFN are relevant research candidates. However, the current repository documents commercial-use restrictions for recent model weights. Do not assume an open-source code license makes every checkpoint free for your production business. [Nature](https://www.nature.com/articles/s41586-024-08328-6)

### Learn separate quantities

Estimate:

```text
Probability of human pickup
Probability of reaching the intended person
Probability of a useful next step
Expected rep time and attempt cost
```

Inputs include local time, lead segment, endpoint evidence, source freshness, attempt history, elapsed time, caller-ID health, and requested callbacks.

Then use a constrained policy to choose timing, retry intervals, and approved script or voicemail treatments.

### Coverage remains a hard constraint

Every eligible lead gets a coverage deadline and an appropriate attempt policy.

Low predicted pickup probability may change *when* someone is called. It cannot postpone them indefinitely.

Requested callbacks take priority. Keep a small randomized exploration share among otherwise reasonable choices, log treatment probabilities, and retain a stable control cohort. Contextual-bandit implementations such as Vowpal Wabbit provide an established framework for this later stage. [Vowpal Wabbit](https://vowpalwabbit.org/docs/vowpal_wabbit/python/latest/tutorials/python_Contextual_bandits_and_Vowpal_Wabbit.html)

**Optimize unique right-party reach and held meetings per original eligible cohort.** Otherwise, the model may become extremely good at calling the same easy people while congratulating itself.

### ConnectRate-style capabilities

We can own the normalization, endpoint correction, historical contactability model, timing policy, and number-health analysis.

We cannot manufacture ConnectRate’s proprietary cross-customer history or licensed network signals with an LLM. Its public descriptions establish feature categories, not a reproducible algorithm or guaranteed equivalent lift. [ConnectRate](https://connectrate.ai/)

---

## 6. The call flow, including voicemail

### Normal calls

The rep is ready before the prospect is dialed. The backend reserves the lead and number, checks permission, and places the call.

Telnyx supports linking the prospect leg to an existing agent leg and bridging on answer. That allows the carrier to perform the bridge without waiting for our answered-webhook round trip. One important implementation detail: `bridge_intent` can change the selected caller ID, so it must not be enabled blindly. [Telnyx](https://developers.telnyx.com/api-reference/call-commands/dial)

Do not wait for unnecessary machine classification before letting a ready rep respond.

### iOS screening

Your single-threaded rep can read a prepared, truthful name-and-purpose introduction. Autonomous screening speech is not required initially.

Still distinguish screening from a successful human connection. The application needs accurate outcomes even when the rep handles the interaction.

### Voicemail and move-on

The proposed behavior is:

```text
Actual mailbox identified
    ↓
Recording opportunity established
    ↓
Separate prerecorded-message permission verified
    ↓
Approved audio begins on prospect leg
    ↓
Rep released to next lead
    ↓
Backend completes playback and ends old leg
```

The old call remains active and billable. Telnyx supports preserving a leg after the other disconnects and playing uploaded audio assets. [Telnyx](https://developers.telnyx.com/api-reference/call-commands/bridge-calls)

**Do not enable unattended drops until false-machine and late-human cases pass testing.** Otherwise, retain a monitored one-click drop.

Prerecorded-message permission is separate from permission to place a live call. Campaign rules must account for applicable consent and opt-out requirements before this feature is enabled. [eCFR](https://www.ecfr.gov/current/title-16/chapter-I/subchapter-C/part-310/section-310.4)

Adaptive voicemail should choose among approved treatments and optimize subsequent callbacks or conversions, not simply count completed playbacks.

---

## 7. The live coach: Groq for reasoning, not transcription

### Speech recognition

I would benchmark **Soniox through Telnyx** first, with Parakeet as the cheaper alternative.

Telnyx documents interim results for Soniox and final-only results for Parakeet. Current published rates are **$0.002/minute** and **$0.0015/minute**, respectively. That makes transcription inexpensive enough to test before maintaining local speech models on every rep’s computer. [Telnyx](https://developers.telnyx.com/docs/voice/programmable-voice/speech-to-text)

Test names, negation, objections, opt-outs, timing, and speaker attribution, not just average transcription accuracy.

Separate rep-side and remote-side audio where supported. Multiple people sharing one remote phone still require uncertain speaker attribution; a transport channel is not a verified person.

### Coaching pipeline

```text
Stable transcript turn
    ↓
Update compact conversation state
    ↓
Retrieve relevant approved playbook cards
    ↓
Groq produces one short structured suggestion
    ↓
Validate evidence and freshness
    ↓
Show or discard
```

Benchmark Groq’s GPT-OSS 20B against 120B. Both support strict structured output, although the documented strict mode currently does not combine with streaming or tool use. For a tiny card, non-streaming JSON is appropriate. [GroqCloud](https://console.groq.com/docs/models)

Example:

> **Ask:** “What would need to improve before you would consider changing providers?”  
> **Reason:** They mentioned an existing provider, but no unmet need has been established.

The model receives recent dialogue, a compact conversation state, verified lead facts, and a few relevant approved cards. **No web searches in the live guidance path.**

My proposed service objective is useful guidance within **2.5 seconds at the 95th percentile** after a qualifying turn. That is a target to test, not a provider guarantee.

Show one card at a time. Cancel stale suggestions. When the model fails, show an appropriate static card or nothing. Silence is better than confidently interrupting the wrong conversation.

### Voice and emotion

Use observable behavior and explicit language: interruptions, turn-taking, long monologues, stated objections, and expressed preferences.

Do not turn voice tone into employee honesty, personality, stress, or emotion scores. Besides being unreliable as evidence, workplace emotion inference is prohibited in covered EU circumstances, with narrow exceptions. [AI Act Service Desk](https://ai-act-service-desk.ec.europa.eu/en/ai-act/article-5)

---

## 8. Gong-like post-call intelligence

Gong’s documented approach includes transcript-based scorecards, AI answers, human correction, and concept-based trackers. Those are useful patterns to implement, not a reason to trust an unreviewable “sales skill: 83” number. [Gong](https://help.gong.io/v1/docs/gong-ai-for-scoring)

The full system should produce:

**Evidence-linked notes:** Problems, impact, objections, competitor mentions, commitments, next steps, and unresolved questions.

**Reviewable scorecards:** Identity and relevance, listening, discovery, business impact, objection handling, factual accuracy, and next-step quality.

**Cross-call memory:** What this prospect previously said, what was promised, and what should change in the next script.

**Team intelligence:** Common objections, recurring factual mistakes, missed commitments, useful snippets, and outcome-linked playbook experiments.

Score only behaviors for which the call provided an opportunity. Use `N/A` and `INSUFFICIENT_EVIDENCE`; do not punish the rep for a wrong number or a two-second hangup.

Every substantive score needs transcript evidence. The rep can inspect and challenge it. Rubrics and models are versioned so a scoring-model update does not masquerade as a sudden change in rep performance.

Post-call processing should create a quick draft, reconcile the final transcript later, and **never overwrite the rep’s edits silently**.

---

## 9. Costs and implementation order

The Markdown contains a worked monthly example totaling **$205.35 before exclusions**, using stipulated volumes for numbers, calling, transcription, research, scripts, searches, coaching, and post-call processing. It is a calculation, not a forecast.

One revealing line item: **20,000 short Groq coaching requests**, each assumed to use 1,500 input and 100 output tokens, calculate to about **$2.85** at the cited GPT-OSS 20B rates. Meanwhile, 5,000 OpenAI search-tool calls cost **$50 before applicable content tokens**. [GroqCloud](https://console.groq.com/docs/models)

So yes, the reasoning can be remarkably cheap. Search, telephony, authoritative data, engineering, and reliability are where the bill becomes less philosophical.

The example excludes DNC access, some optional carrier services, backups, taxes, engineering, and any additional leg/track billing. Actual Telnyx call-detail records must validate the cost model. [Telnyx](https://telnyx.com/pricing/voice-api)

### Build in this order

| Stage | Deliverable and release condition |
|---|---|
| **1. Integration proof** | Tauri audio, Telnyx credentials, caller ID, bridge behavior, transcription timing, and supported traffic pattern validated. |
| **2. Reliable dialer** | Shared pools, permissions, reservations, callbacks, opt-outs, durable events, and honest outcomes. |
| **3. Preparation pipeline** | Multi-lead research, evidence validation, batch scripts, caching, and rep packets. |
| **4. Conversation intelligence** | Transcript review, notes, trackers, scorecards, and human correction. |
| **5. Live guidance** | Groq cards tested for grounding, usefulness, latency, and distraction. |
| **6. Adaptive optimization** | Calibrated models and controlled treatment experiments, with guaranteed coverage. |
| **7. Advanced automation** | Detached voicemail and neural challengers only after their specific acceptance tests pass. |

Tauri compatibility is an explicit early gate because Windows, macOS, and Linux use different embedded webviews. A successful Chrome test is not sufficient. [Tauri](https://v2.tauri.app/reference/webview-versions/)

Also obtain Telnyx approval for the intended traffic pattern: its published short-duration-call policy is relevant to outbound dialers that quickly disconnect machine answers. [Telnyx Help Center](https://support.telnyx.com/en/articles/1130707-what-are-short-duration-calls)

## Bottom line

**The right system is a reliable calling platform with adaptive intelligence around it, not a collection of agents allowed to improvise telephone operations.**

The full design keeps the desktop responsive, centralizes only what must be consistent, researches leads in advance, provides brief evidence-grounded live help, generates reviewable notes and scores, and learns better treatment for the entire eligible list.

The inexpensive part is generating text. The valuable part is ensuring that the system calls the right person, at an appropriate time, under the right identity, and does not waste the human answer when it arrives.