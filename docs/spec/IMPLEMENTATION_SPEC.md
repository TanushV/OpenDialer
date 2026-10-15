# Adaptive Telnyx Sales Dialer: Tauri Implementation Specification

**Version:** 1.0  
**Research checked:** October 14, 2026  
**Deployment model:** Local desktop clients, one small authoritative backend, externally hosted telephony and model inference  
**Initial operating assumption:** U.S. live-rep outbound sales, one active live prospect per rep  
**Document status:** Implementation specification, not an already built, deployed, legally approved, or load-tested application.

## Navigation

[Decisions and current capabilities](#1-executive-decisions) · [Hosting](#3-hosting-and-operating-budget) · [Architecture](#4-architecture-and-trust-boundaries) · [Desktop](#5-desktop-product-specification) · [Data model](#6-data-model) · [Validation and DNC](#7-compliance-and-phone-number-verification) · [Research and scripts](#8-research-and-script-generation-through-openai) · [Numbers](#9-shared-regional-numbers-and-reputation) · [ML](#10-adaptive-scheduler-and-ml-implementation) · [Call control](#11-telnyx-call-control-and-state-machine) · [Voicemail](#12-voicemail-handoff) · [Speech](#13-speech-recognition-and-live-conversation-state) · [Live coach](#14-groq-live-guidance-engine) · [Post-call intelligence](#15-gong-like-post-call-intelligence) · [API contracts](#16-backend-services-and-api-contracts) · [Build sequence](#17-repository-layout-and-build-sequence) · [Security](#18-privacy-security-and-abuse-resistance) · [Costs](#19-cost-model) · [Implementation gates](#20-implementation-phases-and-acceptance-gates) · [Test matrix](#21-minimum-test-matrix) · [Metrics](#22-launch-metrics-and-operating-rules) · [Integration checks](#23-decisions-that-require-account-level-validation-before-code-is-finalized) · [Sources](#24-source-register)

## 1. Executive decisions

Build a **Tauri v2 desktop application** with a **Python/FastAPI backend, PostgreSQL, a durable job queue implemented in PostgreSQL, and separate lightweight worker processes**. Deploy the backend initially on one VM. Do not introduce Kubernetes, Kafka, a paid agent-orchestration platform, a separate vector database, or a self-hosted large language model.

The backend is authoritative for permission to call, lead reservations, number allocation, call state, callbacks, script versions, and pooled learning. Desktop clients cache upcoming work and render the calling experience. The intended conversation media path is directly between the desktop, Telnyx, and the prospect, not through the application VM.

| Area | Selected implementation |
|---|---|
| Desktop | Tauri v2, React, TypeScript, Rust for narrowly scoped native functions |
| Telephony | Telnyx WebRTC SDK plus backend Voice API/Call Control |
| Backend | FastAPI, Pydantic, SQLAlchemy, Alembic, PostgreSQL |
| Shared jobs | PostgreSQL-backed durable jobs with leases, priorities, retries, and dead-letter states |
| Research and scripts | Central OpenAI Responses API adapter; optional official ChatGPT-plan adapter when eligible |
| Live coaching | Groq text inference, initially benchmark `openai/gpt-oss-20b` and `openai/gpt-oss-120b` |
| Live speech recognition | Benchmark Soniox through Telnyx first; replaceable STT interface |
| Low-cost transcription alternative | Parakeet through Telnyx when final-only transcripts meet latency needs |
| Notes and scorecards | Central OpenAI inference, with transcript evidence and human review |
| Scheduling ML | Pooled statistical baseline, then calibrated gradient boosting; constrained experimentation |
| Number strategy | Owned, registered, callback-capable regional pools; stable prospect-to-number assignment |
| Voicemail | Normal-call, permissioned drop into an actual mailbox; independent prospect-leg completion |
| Initial screening treatment | Live rep reads a prepared name-and-purpose introduction; no automatic screening voice required |
| Free hosting candidate | Oracle Always Free A1, subject to capacity and reclamation limitations |
| Paid reference deployment | A 2-vCPU/4-GiB VM; $24/month on the cited DigitalOcean plan, before extras |

**Primary objective:** Increase unique right-party connections and held meetings per original eligible lead cohort while meeting coverage obligations.

**Not acceptable:** Quietly excluding low-propensity leads, fabricating caller identity, treating DNC checks as guesses, inventing personalization, or optimizing the dashboard by counting voicemail as a connection.

Every numeric timeout, threshold, concurrency setting, and model-promotion criterion proposed below is an engineering starting point unless explicitly identified as a provider limit or legal rule.

## 2. What current technology supports, and what it does not

### 2.1 Conversation intelligence

Gong's documented scorecard workflow uses transcript-based questions, AI-suggested or automatic answers, and human overrides. Its AI trackers identify concepts even when the exact wording differs. These are reproducible functional patterns: evidence extraction, concept detection, rubric evaluation, and a review interface. They are not evidence that one generic prompt reproduces Gong's entire data, evaluation, integration, or coaching system.[^gong-scoring][^gong-trackers]

Build beyond basic summaries with **cross-call memory, live approved guidance, evidence-linked scorecards, versioned playbooks, outcome-linked experiments, and corrections that improve future evaluations**. Do not advertise superiority before comparing real outcomes.

### 2.2 Statistical optimization

Individualized contact scheduling has published precedents, including research on improving right-party contacts in outbound collections. That establishes feasibility, not a transferable sales pickup guarantee.[^rpc-paper]

Tabular foundation models are a serious neural alternative. The 2025 TabPFN paper establishes strong small-data results; the current project defaults to TabPFN-3.5, but recent model weights have non-commercial licensing and the maintainers recommend GPU resources for best performance. Therefore, do not make the newest checkpoint a free production dependency. An explicitly permitted older checkpoint or a licensed newer model can be an offline challenger later.[^tabpfn-paper][^tabpfn-repo]

### 2.3 Voice and emotion

Speech recognition produces words, sometimes timestamps and speaker labels. It does not establish a person's underlying emotional state, sincerity, or personality. Separate channels identify call sides, not every individual sharing a speakerphone.

Use observable measures such as turn timing, long monologues, overlap, explicitly stated objections, and expressed preferences. Do not create voice-derived employee emotion, honesty, stress, or personality scores. The EU AI Act prohibits workplace emotion inference in covered circumstances, with narrow medical/safety exceptions.[^eu-emotion]

A multimodal audio model can be evaluated later for concrete tasks such as identifying overlapping speech or preserving prosodic context. It is not necessary for the first useful coach, and it must pass the same accuracy, privacy, cost, and legal gates.

### 2.4 ConnectRate-style capability, without pretending to own its data

ConnectRate advertises validation, carrier/network checks, answer-propensity scoring, and best-time recommendations, informed by its historical calling data. Its public materials do not disclose enough to reproduce the proprietary system or establish equivalent lift on our entire retained cohort.[^connectrate]

Our implementation covers normalization, endpoint correction, optional carrier lookup, cached company research, first-party answer models, adaptive timing, number-health monitoring, and whole-list coverage. Historical outcomes become our own contactability dataset. Licensed carrier signals, ownership databases, and another vendor's cross-customer history are not created by adding an LLM.

Evaluate these components on randomized portions of the original eligible list with equivalent coverage. Report both per-attempt connection and unique-cohort reach. Keep low-propensity leads eligible for appropriate attempts; retain bad-endpoint leads for correction rather than repeatedly calling a confirmed wrong party.

## 3. Hosting and operating budget

### 3.1 Free VM option

Oracle's current Always Free documentation specifies A1 allowances equivalent to **2 OCPUs and 12 GB memory**, with **200 GB combined block storage**. Availability depends on the home region and capacity. Oracle also documents reclamation of idle instances. Do not rely on older 4-OCPU/24-GB tutorials or generate fake load to evade the rules.[^oracle]

**Proposed Oracle configuration:** One ARM64 VM, 2 OCPUs, 12 GB memory, a modest boot volume, encrypted persistent data, and backups outside the VM. Build ARM64-compatible backend images. Desktop installation packages are built separately on appropriate CI/build machines.

Use this for development and an explicitly risk-accepted pilot. It is not high availability. A reclaimed VM can stop new calls and live guidance even if existing carrier media continues.

### 3.2 Paid fallback

The cited DigitalOcean Basic plans list 1-vCPU/2-GiB at **$12/month** and 2-vCPU/4-GiB at **$24/month**. Treat the first as a tightly constrained pilot and the second as a more comfortable initial reference, not a proven capacity guarantee. Backups are extra.[^do]

A comparable VM from another supplier is acceptable. Pick the region using measured desktop-to-backend and backend-to-provider latency rather than price alone.

### 3.3 What runs on this VM

Run the API, database, call-state reducer, lightweight live-coach orchestration, research/script orchestration, scheduler, small-model scoring, and constrained offline training. OpenAI, Groq, and Telnyx perform their own inference or media processing externally.

Do not run a large speech model, a fleet of browsers, or a GPU-sized LLM beside the call controller on a small VM.

Start with separate processes and bounded resources:

| Process | Initial proposed resource policy |
|---|---|
| API and webhook ingress | Highest priority, no CPU-heavy jobs |
| Live-state/coaching worker | Dedicated queue and process; deadline-driven |
| Call-control worker | Dedicated priority lane; never waits behind research |
| Research/script worker | 2 concurrent work units initially; async network requests inside limits |
| Post-call worker | 2 concurrent analyses initially; bounded tokens |
| ML training | 1 job, off-peak, low CPU priority, explicit memory limit |
| PostgreSQL | Reserved memory and limited connections; data on persistent volume |

On a 2-GiB deployment, reduce worker counts, avoid browser rendering, and move training to an admin workstation if necessary. These settings require load testing with the actual campaign.

### 3.4 Availability and failure policy

A single VM is intentionally a single point of failure. Design recovery instead of pretending otherwise:

- Existing conversations should not depend on the application API remaining healthy, but validate actual Telnyx session behavior.
- New pooled calls fail closed if shared authorization or reservations cannot be checked.
- Coaching fails open to cached approved cards; telephony continues.
- Scripts and notes can be viewed from the local cache when offline.
- Desktop opt-outs recorded during an outage are locally persistent. The client stays unavailable for further outbound work until those events synchronize.
- Carrier-side maximum call duration and a recovery sweep protect orphaned legs. An application-only watchdog is insufficient when the VM itself is down.
- Preserve daily encrypted backups plus an append-only off-host authorization/opt-out audit journal where feasible. Reconcile these before restoring outbound service after a backup rollback.

## 4. Architecture and trust boundaries

```text
                         CONTROL AND INTELLIGENCE
Tauri desktop ── HTTPS/WebSocket ── Caddy ── FastAPI
                                           │
                                           ├── PostgreSQL: authoritative state
                                           ├── Call-control/event worker ── Telnyx APIs
                                           ├── Live coach worker ── Groq text LLM
                                           ├── Research/script worker ── OpenAI
                                           ├── Post-call worker ── OpenAI
                                           ├── Scheduler and ML worker
                                           └── Compliance dataset importer

                               MEDIA
Rep headset / Tauri WebRTC ⇄ Telnyx media ⇄ Prospect telephone
                                 │
                                 ├── Permitted STT → transcript events → backend
                                 ├── Permitted recording → secured recording object
                                 └── Permitted voicemail playback on prospect leg
```

Telnyx documents browser-agent applications with individual credentials and backend-issued tokens. Use that pattern rather than embedding the account API key in the desktop.[^telnyx-contact-center]

### 4.1 Required boundaries

**Desktop:** No master carrier, OpenAI, Groq, registry-download, or infrastructure credentials. Store only scoped application tokens, short-lived telephony credentials, and minimum necessary cached data.

**Backend:** Only trusted call-control code can issue outbound commands. LLM output never becomes an unrestricted telephony command, SQL statement, shell command, URL fetch, or permission override.

**Carrier account:** Limit outbound destinations and routes, separate development from production, and configure desktop telephony credentials so arbitrary direct calls cannot bypass the server's policy gate. Prove this with an attempted bypass test. UI restrictions alone are not security.

**Seller isolation:** Every sensitive object includes `tenant_id` and, where different, `seller_id`. Pooling reps for one business is allowed by design; mixing identities or consent across unrelated businesses is not.

### 4.2 Tauri platform gate

Tauri uses different system webviews on Windows, macOS, and Linux. Therefore, working in Chrome does not prove that Telnyx WebRTC works in every Tauri build.[^tauri-webviews]

Start with a Windows test build if that matches the rep fleet. Test microphone permission, audio output selection, Bluetooth devices, incoming calls, reconnection, sleep/wake, and long conversations. Then certify other operating systems. If a platform's embedded WebRTC fails, evaluate a supported native SIP/media component before promising support; do not silently turn the product into an Electron app.

Use Tauri capabilities to restrict native commands. Do not display untrusted research websites in a privileged application webview.[^tauri-capabilities]

## 5. Desktop product specification

### 5.1 Rep workspace

**Session header:** Availability, headset status, connection quality, assigned campaign, current regional identity, and a clear pause control.

**Lead panel:** Verified identity fields, local time and confidence, previous outcomes, research facts with source links, and next-step commitment.

**Script panel:** Short opener, two or three discovery prompts, objection cards, and the prepared screening introduction. Show script version and allow a rep to flag inaccurate personalization.

**Live panel:** Speaker-labeled transcript, one coaching card, relevant approved proof points, and an urgent-help hotkey. Keep it readable during speech; do not make the rep operate a scrolling dashboard while listening.

**Call controls:** Dial, mute, keypad/extensions, pause after call, explicit opt-out, wrong number, callback, and permissioned voicemail drop. Hangup and opt-out must remain accessible when an AI service is down.

**Wrap-up panel:** Immediate disposition, a short draft note, requested callback, and automatic analysis status. Long analysis must not block the next authorized call.

### 5.2 Review workspace

A call page contains synchronized transcript and recording when permitted, timeline markers, extracted commitments, notes, scorecard dimensions with evidence, rep corrections, and manager comments. Include a filter for low-confidence or contested AI results.

### 5.3 Manager/admin workspace

Implement lead import, campaign policies, seller identity, playbook editor, source permissions, DNC dataset status, number inventory/health, coverage backlog, experiment reports, cost budgets, model registry, and access controls.

Do not rank reps on raw pickup rates when lead mix and caller-ID treatment differ. Keep controllable conversation behaviors separate from pre-answer delivery outcomes.

## 6. Data model

Use UUID primary keys, UTC event timestamps, explicit IANA time zones, immutable event IDs, and database constraints. The following is the logical schema, not a complete migration.

| Table/group | Important fields |
|---|---|
| `tenants`, `sellers`, `users`, `memberships` | Identity, roles, seller permissions, disabled status |
| `campaigns`, `policy_versions` | Purpose, jurisdiction scope, legal review, cadence, allowed treatments, budgets |
| `companies`, `leads` | Source IDs, company domain, segment, rep responsibility, lifecycle, coverage due |
| `phone_endpoints`, `lead_phone_links` | E.164, extension, evidence, ownership status, line type, carrier, last verification |
| `consent_evidence` | Subject, seller, endpoint, scope, acquisition method, timestamp, evidence object, revocation |
| `suppressions` | Seller/tenant scope, endpoint/person scope, reason, origin, effective time, immutable history |
| `registry_subscriptions`, `registry_snapshots` | Seller SAN reference, authorized NPAs, dataset version, freshness, checksum |
| `compliance_decisions` | Attempt intent, allow/deny, reasons, policy version, evidence and dataset versions |
| `number_inventory`, `number_health` | DID, seller, region, registration, lifecycle, health evidence, capacity policy |
| `lead_number_bindings`, `number_leases` | Stable assignment, lease owner, lease expiry, active call association |
| `call_attempts`, `call_legs` | Rep, lead, endpoint, DID, provider IDs, state, timing, disposition, billing units |
| `provider_events`, `command_outbox` | Provider event ID, payload hash, durable receipt, dedupe key, command status |
| `transcript_segments` | Call, side/speaker, event/segment ID, revisions, timestamps, text, finality, confidence |
| `research_sources`, `research_facts` | URL, title, retrieval time, hash, lead/company link, fact status, expiry |
| `playbooks`, `script_versions`, `treatment_variants` | Approved claims, schema, content, provenance, version, review status |
| `coach_events`, `conversation_states` | Trigger, source segments, suggested card, state version, display/use feedback |
| `notes`, `scorecards`, `scorecard_items` | Transcript version, evidence, rubric version, AI/human author, overrides |
| `followup_tasks`, `commitments` | Owner, due time, evidence, status, external CRM reference |
| `feature_snapshots`, `model_versions` | As-of features, trained-through time, metrics, checksum, activation |
| `experiment_assignments`, `policy_decisions`, `outcomes` | Unit, arm, allowed actions, chosen action, propensity, delayed reward |
| `jobs`, `usage_ledger`, `audit_events` | Retry state, leases, provider usage, cost attribution, administrative changes |

### 6.1 Essential invariants

- One active dialing reservation per lead and per callable endpoint within the relevant seller scope.
- One live prospect obligation per rep. A detached voicemail job does not count as a rep conversation but retains its own call resources.
- A DID cannot be used outside its seller identity and authorized inventory.
- An opt-out is never overwritten by a later CSV import.
- A completed or failed command is not resubmitted under a new idempotency key merely because a worker restarted.
- Call state is not inferred from the ordering of webhook arrival alone.
- A human edit is preserved when an improved AI note arrives.
- Every personalized factual claim is linked to a research fact or explicitly marked as an unverified hypothesis that must not be spoken as fact.

## 7. Compliance and phone-number verification

Have qualified counsel approve the campaign policy configuration before production. This section defines implementation controls, not a universal legal determination for every destination or campaign.

### 7.1 The validation pipeline

```text
Import → normalize → deduplicate → validate format/range plausibility
       → resolve endpoint evidence → authorized DNC/suppression check
       → determine campaign permission → research/scheduling eligibility
```

Use `libphonenumber`/a maintained port for formatting and plausibility. Its maintainers explicitly distinguish validation from proof of assignment, reachability, or ownership.[^libphonenumber]

Use cached Telnyx Number Lookup only where carrier or line-type information changes a decision. It is enrichment, not an identity or DNC certification.[^telnyx-lookup]

Represent these states separately:

```text
format_status: valid | invalid | unknown
assignment_evidence: unknown | plausibly_assigned | disconnected_evidence
ownership_status: unknown | likely_match | confirmed_match | wrong_party
reachability_status: unknown | previously_reached | transient_failure | invalid_endpoint
calling_permission: allowed | denied | needs_review
```

Do not send verification texts or place artificial test calls just to manufacture an “exists” flag. Use legitimate contact evidence and authorized data sources.

### 7.2 National and internal DNC

The U.S. National Registry requires authorized access and current data. FTC guidance specifies synchronization at least every 31 days and describes authorized change-list downloads. Maintain seller-specific subscription authorization and do not treat a third party's scraped “DNC API” as proof of compliance.[^ftc-guide]

Current 16 CFR 310.8 lists the first five area codes at no charge, then $85 per additional area code, capped at $23,425 annually. Access fees cannot be pooled across unrelated seller clients. Registry data is restricted to compliance or preventing calls, not lead enrichment or model targeting.[^dnc-fees]

Implement:

1. An admin registers the seller and establishes the required authorized access. Where enrollment is manual, document that onboarding step rather than automating around it.
2. Import a full authorized dataset into staging, verify checksums/counts/format, then atomically publish the snapshot.
3. Apply supported change lists on a scheduled basis, normally daily when available, respecting the registry's access conditions.
4. Track freshness and authorized area-code coverage per seller.
5. Return `NEEDS_REVIEW` or `DENY` when required coverage is missing or stale. Absence of data is not clearance.
6. Check internal seller-specific opt-outs first and immediately propagate newly received opt-outs.
7. Add state-list adapters and counsel-configured rules where applicable. National DNC alone is not the complete U.S. compliance layer.

**Small-server storage:** For U.S. ten-digit numbers, an exact bitset of the seven-digit suffix per area code needs 10,000,000 bits, or 1.25 MB before metadata. A compressed bitmap can be smaller. This is a proposed implementation calculation, not a registry format. Retain dataset provenance and seller authorization separately. Bloom filters may accelerate negatives but must not create false “cleared” decisions or replace an authoritative check.

Do not use registry membership as an ML feature. The model receives only the already-eligible candidate set.

### 7.3 Reassigned numbers

Use the Reassigned Numbers Database selectively where a documented prior contact/consent date makes the query meaningful. The RND supports batch/API querying after authorized registration and subscription. A reassignment/disconnection check is different from DNC, ownership identification, or a general live-phone probe.[^rnd]

Store query date, reference date, result, and subscription context. Route uncertain results to review rather than claiming verified ownership.

### 7.4 Pre-call policy gate

Immediately before an outbound command, evaluate:

```text
seller identity and campaign approval
AND current internal suppression state
AND applicable national/state DNC policy
AND valid endpoint eligibility
AND permitted local calling time, including timezone uncertainty
AND campaign cadence and requested-callback policy
AND rep ready and no conflicting reservation
AND healthy authorized caller ID
AND provider/account spending and route limits
```

Maintain separate permissions for live calls, prerecorded/artificial speech, recording/transcription, and follow-up messaging. A permission for one is not permission for all. Federal rules address prerecorded telemarketing and opt-out mechanisms; additional state and destination rules still apply.[^tsr-abusive][^fcc-rules]

If time zone is uncertain, use a conservative intersection of plausible permitted windows or require review. A caller's area code is not sufficient proof of location.

### 7.5 Opt-outs and retention

A clear request not to call should trigger immediate local and central suppression. Use deterministic/manual controls plus a high-recall transcript trigger that can conservatively place the endpoint on temporary hold. False-positive holds can be reviewed; no LLM can automatically remove an opt-out.

Required compliance records have their own retention policy. Current 16 CFR 310.5 generally specifies five years and separately addresses retention of scripts and unique prerecorded messages from their last use. Do not confuse this with a requirement to retain every raw recording indefinitely.[^recordkeeping]

Retain legally required evidence while minimizing optional audio and transcript retention. Apply legal holds explicitly.

## 8. Research and script generation through OpenAI

### 8.1 Two legitimate authentication modes

**Production default: OpenAI project/service-account API credentials.** Put credentials only on the backend, impose project and internal budgets, and use the Responses API. This route supports predictable organization-level ownership and a separate Batch integration.

**Optional: official Sign in with ChatGPT plan usage.** Current OpenAI documentation supports eligible open-source/local apps and provides a self-hosted VM flow. It is not permission to pool one consumer account as an unlimited team inference service. Paid/remotely hosted distribution has separate eligibility considerations.[^siwc-overview][^siwc-vm]

The plan-usage route uses the public Responses API, account-available model slugs, `store:false`, and `stream:true`. Current preview limitations differ from API-key requests and omit several common fields; web search remains subject to model/account policy.[^siwc-inference][^siwc-limits]

Implement separate adapters. Do not point either adapter at undocumented ChatGPT `backend-api` endpoints, share browser cookies, or reuse one user's token across unrelated users. Keep metered-API fallback explicit and budget-authorized.

### 8.2 Research is a pipeline, not an unrestricted agent swarm

Use jobs that contain multiple leads, but isolate evidence and outputs by lead/company ID. Proposed starting work unit: **20 leads**, grouped by company domain where possible. Run several units concurrently within provider and server limits.

```text
Eligible lead cohort
  → group by company/domain
  → reuse fresh company research
  → bounded source discovery
  → retrieve permitted pages
  → extract structured facts
  → verify citations and entity matching
  → write or modify scripts
  → validate claims and format
  → publish versioned lead packets
  → prefetch into assigned rep's desktop
```

Sharing a work unit does not mean giving a model 20 unrelated people and accepting an unkeyed narrative. Every response must map back to exact input IDs.

### 8.3 Source discovery and SEO-related research

Default source order: existing authorized CRM data, official company site, relevant publicly accessible first-party pages, then an approved search tool. Use OpenAI web search where available to avoid another search-provider subscription. Search is separately metered on the API route.[^openai-websearch][^openai-pricing]

For SEO-oriented selling, extract observable business facts such as services, locations, stated target customers, page titles, indexable content, and visible contact paths. Do not invent rankings, traffic, ad spend, backlink counts, or revenue. Those need specific measurement or licensed data.

Proposed per-company budget: up to two searches and three useful pages initially, unless a campaign explicitly authorizes deeper work. Cache stable company facts for a proposed 30 days; give change-sensitive facts shorter lifetimes. A timestamp is not proof that the underlying statement is still true, so mark source dates and uncertainty.

### 8.4 Fetch security

The fetcher runs outside the privileged call-control process. Allow only permitted HTTP(S) destinations. Reject loopback, private/link-local addresses, metadata services, unsafe ports, oversized responses, unbounded redirects, and disallowed content types. Resolve and validate DNS at each relevant hop to mitigate rebinding.

Treat website text as untrusted data, not instructions to the agent. It cannot request secrets, add tools, alter calling policy, or issue messages. Honor source access restrictions; do not bypass authentication or CAPTCHA challenges. Use a browser renderer only for an allowlisted need and with very low concurrency.

### 8.5 Research output contract

```json
{
  "lead_id": "uuid",
  "company_id": "uuid",
  "researched_at": "2026-10-14T15:00:00Z",
  "facts": [
    {
      "fact_id": "fact_uuid",
      "statement": "The company's services page lists commercial roof repair.",
      "source_id": "source_uuid",
      "source_date": null,
      "entity_match": "confirmed_company_domain",
      "status": "supported",
      "expires_at": "2026-11-13T15:00:00Z"
    }
  ],
  "hypotheses": [],
  "unknowns": ["Current provider", "Current budget"],
  "conflicts": []
}
```

This is an application schema. Source URLs and retrieved text live in `research_sources`. `status` is backed by validation and evidence, not merely the model's self-reported confidence.

### 8.6 Script writer/modifier

Inputs: lead packet, approved product claims, campaign objective, call stage, previous verified commitments, preferred language, and experiment variant.

Output:

```json
{
  "lead_id": "uuid",
  "script_version": "uuid",
  "playbook_version": "uuid",
  "opener": "...",
  "screening_intro": "...",
  "discovery_questions": ["...", "..."],
  "objection_cards": [{"objection": "...", "response": "..."}],
  "voicemail_variant_id": null,
  "fact_ids_used": ["fact_uuid"],
  "prohibited_claim_checks": [],
  "ready_for_rep": true
}
```

Apply deterministic checks: required IDs, approved seller identity, maximum lengths, factual citations, banned guarantees, and no invented prior relationship. Use a second model review only where risk or novelty justifies it. If evidence fails, publish a non-personalized approved fallback rather than delaying every call.

The modifier changes scripts when new verified information or an approved playbook version arrives. Keep the original script and diff. Do not rewrite the active rep's entire screen mid-sentence.

### 8.7 Batch processing and concurrency

Distinguish **our multi-lead work units** from the **OpenAI Batch API**. The latter offers a 50% discount with a completion window of up to 24 hours.[^openai-batch]

Use a rolling preparation horizon with enough time for that window. Use normal bounded-concurrency requests for morning additions and stale-script repairs. Do not wait for Batch immediately before a shift.

Pipeline research first, then submit independent text-only script/analysis requests using `custom_id` values containing the object and version. Join results by ID, never output order. Verify tool/model support before attempting research-tool requests in Batch. The plan-usage adapter must not assume that the Batch API is supported.

Suggested cache key:

```text
hash(lead_evidence_version, company_research_version,
     playbook_version, script_variant, model_snapshot, prompt_version)
```

A single-flight lock prevents multiple reps from paying to research the same company simultaneously. Retries only resubmit missing or failed objects. Persist provider request IDs, token/tool usage, and failure reasons.

### 8.8 Model policy

Use configurable roles rather than hardcoding the newest model everywhere:

- `research_extract`: a low-cost model that passes factual extraction tests.
- `script_write`: a stronger model when it materially improves relevance and correctness.
- `postcall_extract`: low-cost model for routine structured notes.
- `scorecard_review`: stronger model or selective escalation for difficult calls.

Current OpenAI pricing lists GPT-6 Luna and GPT-6.1 Sol as materially different cost tiers. Treat them as candidates; pin an available snapshot after acceptance testing and record it on every output.[^openai-pricing]

## 9. Shared regional numbers and reputation

### 9.1 Allocation policy

Maintain a shared pool per seller, divided into regions and operational states. Reps receive temporary working sets, but the prospect's caller ID remains stable when healthy.

Selection order:

```text
existing healthy lead-number binding
→ healthy number appropriate to known prospect region
→ healthy general business number
→ defer for capacity/policy review
```

A familiar-looking area code is not permission to imply a local office. Use only owned/authorized identities. Account for prospect location confidence and established callback history.

Number leases reserve operational capacity, not a fictitious technical rule that every DID supports only one concurrent call. Initially set conservative application capacity and confirm provider limits separately.

### 9.2 Procurement and registration

An admin approves the seller identity and number-purchase budget. A procurement job can then purchase regional inventory based on forecast demand, register it, test inbound routing, and introduce it gradually.

Free Caller Registry distributes registration data but does not guarantee removal of spam labels or replace monitoring. Telnyx documents attestation based on authorized ownership; attestation should not be treated as a guarantee that a carrier will show a clean label.[^fcr][^stir]

### 9.3 Warmup as legitimate ramping

Use this lifecycle:

```text
purchased → identity_configured → callback_tested → limited_real_use
          → healthy → investigating → paused_for_outbound → remediated
```

Start new numbers at an operator-approved fraction of their expected legitimate workload. Increase only after reviewing real outcomes, complaint signals, route failures, and volume consistency. There is no universal safe calls-per-day number in this specification.

Do not create fake conversations, repeatedly call your own endpoints, or replace identities to evade enforcement. Maintain inbound callbacks while outbound use is paused.

### 9.4 Number-health estimator

Track per-DID and per-route answer outcomes, human classification, failures, short connected calls, opt-outs, callbacks, and any available external reputation observations.

Use a pooled baseline adjusted for lead source, segment, time, attempt number, and endpoint type. A decline on one DID is a reason to investigate, not proof of a spam label. Require enough evidence before changing state; low-volume noise should not automatically trigger new number purchases.

Optional Telnyx Number Reputation currently adds a $100 monthly enterprise fee, $0.10 per fresh check, and $1 per number remediation submission. Our own telemetry is cheaper but not equivalent to that external intelligence.[^telnyx-reputation]

### 9.5 Callback routing

Resolve the called DID to seller identity first. Then match the inbound caller, previous attempts, and responsible rep. Route to that rep when available, otherwise to an appropriate teammate or callback queue. Do not expose another business's records on ambiguous matches.

Keep a retirement/callback-retention policy for numbers before releasing them. Losing callbacks to save one number's rental can damage the very conversions the system is trying to improve.

## 10. Adaptive scheduler and ML implementation

### 10.1 Separate objectives

Predict separately:

```text
P(live human answer | pre-call context)
P(right party | human answer, context)
P(qualified next step | right-party connection, context)
Expected rep time and attempt cost
```

Do not optimize a single “connect score” that mixes correctness, ownership, answerability, and conversion.

Primary reporting uses unique right-party reach and held meetings per original eligible cohort. Calls to gatekeepers and screening assistants get their own outcomes.

### 10.2 Coverage constraints

Every eligible lead has `coverage_due_at`, a campaign attempt entitlement/limit, and `next_attempt_at`. Low predicted probability may move the call to another appropriate slot but may not defer it forever.

Priority classes:

1. Requested callbacks and time-specific commitments.
2. Fresh requested-contact leads under their own policy.
3. Overdue or soon-due coverage obligations.
4. Remaining eligible leads, ranked by expected value and time suitability.

Keep cold outbound and explicit requested contact in separate evaluation cohorts. When planned coverage exceeds available rep capacity, show the projected shortfall. Software cannot satisfy an impossible schedule by hiding it.

### 10.3 Candidate actions

The policy chooses only among pre-approved actions, such as a later local-time slot, a permitted retry interval, an approved script variant, or a permitted voicemail treatment. It does not choose whether to obey an opt-out or whether to use an unauthorized caller ID.

A stable lead-number binding is normally fixed context, not an action to randomize on every attempt.

### 10.4 Features available before dialing

Use source freshness, endpoint evidence, segment, company size band when legitimately known, local weekday/time, previous attempt outcomes, elapsed time, callback history, number-health summary, and rep/campaign capacity.

Exclude future outcomes, call duration not yet observed, post-call transcripts, registry membership, inferred sensitive traits, and cross-tenant data without an appropriate basis.

Store the exact as-of feature vector used for every policy decision so it can be reconstructed later.

### 10.5 Model progression

**Baseline:** Hierarchical or pooled logistic model, or smoothed segment/time-bin rates. Partial pooling avoids confident individual predictions from two missed calls.

**Challenger:** Gradient boosting using a maintained implementation such as scikit-learn's histogram boosting or another commercially usable library. Encode categorical features consistently and calibrate probabilities on a separate validation set.

**Later challenger:** A permitted tabular foundation model or small neural network, only after an out-of-time evaluation shows meaningful value and acceptable operational cost.

Use a simple model while the dataset is sparse. The initial benefit comes from clean outcomes and better scheduling discipline, not neural architecture complexity.

### 10.6 Learning policy

Start with rule-based scheduling plus randomized tests. Then introduce a constrained contextual bandit for eligible timing/treatment choices. Contextual bandits and action-probability logging have established implementations in Vowpal Wabbit.[^vw]

Proposed exploration share: 10% of non-urgent eligible choices, adjustable after review. Exploration never violates consent, cadence, caller-ID stability, or coverage deadlines.

Log:

```text
decision_id, eligible_action_set, context_version,
chosen_action, chosen_action_probability,
policy_version, experiment_arm, outcome_window
```

Use logged propensities for suitable off-policy evaluation only when action support and assumptions hold. Do not pretend observational data makes every policy comparison causal.

### 10.7 Reward and attribution

Use immediate human/right-party labels for answer models. Use a fixed delayed outcome window, such as 14 or 30 days, for callbacks and held meetings. Pending windows are censored, not automatically negative.

Attribute voicemail and script outcomes using randomized treatment where possible. A lead reached after four touches cannot automatically give all credit to the last one.

Do not reward repeated contact with an already-reached easy prospect when the objective is new unique reach. Model uncertainty should encourage measured exploration, not aggressive repetition.

### 10.8 Training, validation, and promotion

Train off-peak from immutable snapshots. Split chronologically, keep repeated leads/endpoints grouped as needed, and test generalization across numbers and reps. Track calibration, Brier score/log loss, segment performance, and downstream cohort outcomes.

Suggested promotion criteria are: no hard-policy violations, no coverage degradation, improved calibrated performance, and an adequately powered online test on the actual business objective. Require manual approval for production activation and retain rollback models.

Use a stable randomized control cohort throughout the rollout. Caller-ID experiments may need cluster assignment by number/pool to avoid contamination.

## 11. Telnyx call control and state machine

### 11.1 Session establishment

1. Rep signs in to the app and selects an authorized campaign.
2. Backend issues a short-lived telephony token for that rep's configured credential.
3. App connects to Telnyx and performs the audio/device checks.
4. Rep declares readiness; backend confirms presence and no live obligation.
5. Scheduler proposes candidates; authorization is checked again at actual dialing time.

Use a server-controlled two-leg design: an agent leg and a prospect leg. A parked/prepared agent leg can reduce answer-to-rep delay, but test its ongoing billed minutes against creating legs per call. The backend must not ring a prospect while the rep's audio path is unavailable.

**Preferred answer path:** Dial the prospect with the ready agent leg in `link_to`, `bridge_on_answer: true`, an explicit authorized regional `from`, and `prevent_double_bridge: true`. Telnyx can then bridge on answer without an application webhook round trip. The double-bridge guard hangs up a conflicting call, so reservations must prevent that conflict in the first place.[^telnyx-dial]

For approved detached voicemail, configure `park_after_unbridge: "self"` on the prospect leg. Set an appropriate `time_limit_secs` as a carrier-side orphan-call safeguard, not a short value that truncates legitimate conversations. Avoid blindly enabling `bridge_intent`: its documented behavior can replace the selected `from` with the linked leg's caller ID. Verify the observed identity and media behavior in account-level tests.[^telnyx-dial]

### 11.2 Attempt lifecycle

```text
QUEUED → RESERVED → AUTHORIZED → DIALING → RINGING
   → ANSWERED_UNCLASSIFIED
      → HUMAN / GATEKEEPER / SCREENING / IVR
      → VOICEMAIL_GREETING → VOICEMAIL_READY → VM_PLAYING
   → BUSY / NO_ANSWER / TEMPORARY_FAILURE / INVALID_ENDPOINT
   → ENDED → DISPOSITION_PENDING → FINALIZED
```

These are our normalized states. Provider events and IDs are stored separately.

Telnyx's answer event can occur for a human or machine. AMD provides separate classifications, and uncertain results should not be treated as certain voicemail. The documented premium screening mode also produces non-final screening events, but it is optional for this live-rep design.[^telnyx-amd]

### 11.3 Human-first behavior

Bridge the ready rep without waiting for unnecessary machine classification. Validate the bridge/early-media behavior against the chosen Telnyx path. Log answer-to-audible-rep timing in tests.

For iOS or another screening service, show the prepared identification line and let the rep answer normally. A single-threaded rep removes the need for autonomous screening speech, not the need to distinguish screening from a successful human connection.

For IVRs, expose keypad/extension entry and a separately approved navigation strategy. Do not count a switchboard menu as the intended prospect.

### 11.4 Idempotency and event ordering

Telnyx documents duplicate, simultaneous, and out-of-order webhook delivery. Verify webhook signatures, durably store each unique event, acknowledge promptly, and process through an idempotent state reducer.[^telnyx-webhooks]

Use deterministic `command_id` values for commands that support them. On an uncertain dial response, reconcile provider state before retrying. A new random idempotency key can create another real call.

Separate source timestamp, receipt timestamp, and reducer sequence. Never allow a late “ringing” event to move a completed call backward.

### 11.5 Call-start transaction

Within a short database transaction, reserve the lead/endpoint and DID capacity, verify policy, and create a durable command intent. The call-control worker rechecks time-sensitive permission immediately before submitting the carrier command. Use a serialized endpoint/seller command stream or equivalent locking/fencing to order opt-outs against pending starts.

Do not hold a database transaction open during an external API request. An opt-out received after an already-submitted carrier command cannot travel back in time; cancel pending work immediately and end or suppress future activity according to policy.

### 11.6 Provider traffic approval

Telnyx defines short-duration connected calls as six seconds or less and documents special treatment and charges when their proportion exceeds its threshold. It also states that it does not support use cases requiring short-duration calls. Obtain written approval for the actual outbound traffic pattern, especially machine-detection hangups.[^telnyx-short]

Never insert fake silence to manipulate billing/reputation metrics.

## 12. Voicemail handoff

### 12.1 Supported product behavior

The rep can move to the next lead while the old prospect leg finishes a permitted voicemail. The old telephone call remains active and billable during playback.

Prerecorded-message permission is checked independently from live-call permission. Where the applicable rules require a prerecorded opt-out mechanism, implement and test it before enabling this feature.[^tsr-abusive]

### 12.2 Exact control pattern

Use Telnyx's bridge behavior to preserve the prospect leg when the agent leg disconnects. The current bridge parameter is **`park_after_unbridge: "self"`** on the relevant leg; do not invent a `hangup_after_unbridge` parameter. Validate this with the actual call topology.[^telnyx-bridge]

Proposed flow:

1. Normal call reaches a sufficiently certain actual mailbox.
2. Wait for the genuine recording opportunity; do not play over the greeting.
3. Backend verifies permission, approved message version, opt-out support, and call state.
4. Establish ownership of the detached-call job and a maximum remaining lifetime.
5. Play the approved audio on the prospect leg using `playback_start`, `target_legs: "self"`, and a deterministic command ID.
6. Only after playback start is confirmed and leg-survival behavior is established, release the rep's old leg and mark the rep ready for a new reservation.
7. On playback completion or failure, end the prospect leg and record the outcome.

Telnyx supports `media_name` for previously uploaded WAV/MP3 assets and provides playback events. This avoids adding another provider just to host reusable voicemail recordings.[^telnyx-playback]

### 12.3 Failure and uncertainty policy

A busy signal, ringing timeout, or network error is not a mailbox. “Playback complete” is not proof that a mailbox saved the recording.

If a late human is detected after detachment, a real recovery destination is required. If every rep is busy, the system cannot guarantee immediate human recovery. Initially use a monitored one-click drop or require sufficiently tested mailbox completion detection; do not promise unattended reliability that the architecture cannot deliver.

Continue any permitted monitoring carrier-side after detachment. The rep's new local media stream cannot hear the old call. Stop playback when appropriate, preserve opt-out handling, and flag false classifications for evaluation.

### 12.4 Adaptive voicemail policy

Experiment among approved treatments: no voicemail, selected voicemail variants, and eligible attempt positions. Limit total voicemail touches by campaign policy. Optimize delayed callbacks, unique reach, and held meetings, not the number of audio files played.

Keep prerecorded assets human-recorded initially. This minimizes another synthesis dependency and gives explicit control over wording and identity.

## 13. Speech recognition and live conversation state

### 13.1 Provider selection

Groq is the **live text reasoning provider**, not the transcription provider in this architecture.

Start by benchmarking **Soniox through Telnyx** against **Parakeet through Telnyx** and, if necessary, a higher-cost engine. Telnyx documents Soniox interim results and endpointing, while its Parakeet integration provides final results only. Current listed transcription rates are $0.002/minute for Soniox and $0.0015/minute for Parakeet; engine and account behavior must be verified on your actual traffic.[^telnyx-stt][^telnyx-pricing]

Do not choose solely by word-error rate. Evaluate recognition of names, amounts, negation, objections, opt-outs, speaker attribution, time-to-stable-turn, and transcript revision behavior.

### 13.2 Audio and speaker handling

Request the needed call tracks through the supported transcription interface. The current action supports `inbound`, `outbound`, and `both`. Confirm that returned events identify the track correctly for the selected engine and topology.[^telnyx-transcription-api]

Preserve these distinctions:

- `rep_side` versus `remote_side` is a transport role.
- `speaker_1`, `speaker_2` are diarization labels, not verified identities.
- `prospect_name` is a separately verified business identity.

When several people share the remote endpoint, mark uncertain attribution rather than assigning every sentence to the target prospect. Add post-call diarization only when needed and permitted. Do not merge both audio channels too early if it destroys reliable role attribution.

### 13.3 Transcript normalization

Normalize provider messages into:

```json
{
  "call_id": "uuid",
  "segment_id": "provider_or_generated_id",
  "revision": 2,
  "side": "remote",
  "speaker_id": null,
  "start_ms": 18200,
  "end_ms": 21400,
  "text": "We already use another provider.",
  "is_final": true,
  "confidence": null,
  "received_at": "2026-10-14T15:00:21.600Z"
}
```

Fields not provided reliably by an engine remain null or explicitly estimated. Do not fabricate precise timestamps or confidence scores. Preserve a revision lineage so a corrected transcript does not leave contradictory permanent coaching evidence.

Track whether each utterance was actually audible to each participant. Exclude hold music, prerecorded voicemail, screening prompts, and system announcements from rep sales-behavior scores.

### 13.4 Conversation state

Maintain a compact state per active conversation:

```text
call stage and participant roles
confirmed problem and business impact
current objection and whether resolved
approved facts already stated
questions already asked and answered
buying/process signals explicitly expressed
next-step commitments
compliance/consent status from the authoritative system
recent 30–60 seconds of stable dialogue
last suggestion and whether it remains relevant
```

Use deterministic updates for timestamps and known events. Use a small structured model extraction only when semantic interpretation is needed. Update at meaningful turn boundaries rather than sending every partial transcript to a model.

State has a monotonically increasing version. Every live suggestion references the state and transcript version that produced it.

## 14. Groq live guidance engine

### 14.1 Selection and request format

Benchmark `openai/gpt-oss-20b` for speed/cost and `openai/gpt-oss-120b` for harder reasoning. Current Groq documentation lists both and supports strict structured outputs for them. It also states that structured-output mode currently does not combine with streaming or tool use.[^groq-models][^groq-structured]

For a tiny guidance card, use non-streaming strict JSON, not a long streamed essay. Still handle refusal, timeout, rate limits, empty content, and transport failures.

Pin an evaluated model/version where available. Do not silently change model behavior during an experiment.

### 14.2 Triggering

Eligible triggers include an explicit objection, an unresolved prospect question, a discovery gap at the appropriate stage, a clearly expressed buying signal, a next-step commitment, and a rep help request.

Use a cheap classifier/rule pass to decide whether a model call is useful. `NO_ACTION` is a valid and frequent result. No prompt is needed merely because another three seconds passed.

Do not trigger guidance from uncertain transcript fragments unless the risk of waiting is higher and the behavior is explicitly designed for it. Compliance holds and manual opt-outs operate outside the coaching model.

### 14.3 Retrieval before generation

Select a few approved playbook cards using deterministic tags and PostgreSQL full-text search first. Add local embeddings only when a measured retrieval problem justifies them. A separate paid vector database is unnecessary at this scale.

The live model receives:

```text
campaign objective + approved claims + current call stage
relevant evidence-backed lead facts
recent stable dialogue + compact conversation state
2–4 relevant approved playbook cards
last suggestion + constraints + output schema
```

No general web searches are permitted in the live coaching path. Research should already be cached.

### 14.4 Output contract

```json
{
  "action": "SHOW_CARD",
  "kind": "DISCOVERY",
  "say_or_ask": "What would need to improve before you would consider changing providers?",
  "reason": "The prospect mentioned an existing provider, but no unmet need is established.",
  "evidence_segment_ids": ["seg_42"],
  "playbook_card_ids": ["existing_provider_03"],
  "state_version": 27,
  "expires_in_ms": 7000
}
```

For `NO_ACTION`, return empty guidance content and preserve the required schema. Validate IDs and length. A self-reported model confidence must not be treated as an independently calibrated probability.

### 14.5 Prompt policy

The core instruction should require the model to help the rep understand the prospect and reach a truthful next step. It must not invent evidence, guarantees, pricing authority, personal familiarity, or a reason to ignore a refusal. It should ask one useful question or supply one approved response, not lecture the rep.

Keep instructions, approved business knowledge, untrusted transcript, and untrusted research clearly separated. A prospect saying “ignore your instructions” is conversation content, not a command to the software.

### 14.6 Live UX and latency budget

Proposed targets, to be measured:

| Stage | Target budget |
|---|---:|
| Endpointing/stable transcript | 300–900 ms after a relevant utterance |
| State update and retrieval | Under 100 ms |
| Groq request and tiny response | 200–900 ms under accepted load |
| Delivery and rendering | Under 150 ms |
| Overall useful-card delivery | p95 under 2.5 seconds after a qualifying turn, subject to STT/network behavior |

These are service objectives, not provider guarantees. Measure separate timers so slow endpointing is not mistaken for slow LLM generation.

Display at most one card. Proposed cooldown: 15–30 seconds between non-urgent cards. Hide expired cards and cancel stale requests when a newer conversation state makes them irrelevant. Prefer a card at a natural pause rather than covering the screen while the prospect speaks.

Maintain at most one active model request per call. Replace queued obsolete work with the newest useful request. Never deliver a backlog of suggestions after the conversation has moved on.

### 14.7 Failure mode

If the model misses its deadline, show a matching approved static card only when it remains useful; otherwise show nothing. Do not block audio, hangup, notes, or the next call. Circuit-break repeated provider failures and show service status unobtrusively.

### 14.8 Live-help evaluation

Create a labeled replay set of real, permissioned calls. Have reviewers identify whether each suggested card was grounded, timely, concise, useful, and appropriate. Include false objections, sarcasm, interruptions, opt-outs, wrong numbers, and very short calls.

Evaluate live help in a rep-balanced randomized trial. Measure qualified conversations and held meetings per comparable cohort, along with distraction and correction rates. Do not infer effectiveness from how many cards the model generates or how often reps click them.

## 15. Gong-like post-call intelligence

### 15.1 Two-pass completion

On call end, immediately commit the telephony outcome and enqueue analysis. Produce a quick draft from available final segments. If the final transcript arrives later, reconcile it and publish a new analysis version.

Never overwrite rep edits. Present an explicit diff or suggested update. Separate “draft,” “AI finalized,” and “human confirmed” status.

### 15.2 Notes schema

```json
{
  "call_id": "uuid",
  "transcript_version": 3,
  "summary": "...",
  "outcome": "callback_requested",
  "problems": [{"text": "...", "evidence": ["seg_8"]}],
  "objections": [{"text": "...", "status": "unresolved", "evidence": ["seg_14"]}],
  "commitments": [
    {
      "owner": "rep",
      "action": "Call back",
      "due_at": null,
      "verbatim_time_expression": "next Tuesday afternoon",
      "evidence": ["seg_19"],
      "needs_time_confirmation": true
    }
  ],
  "unknowns": ["Budget not discussed"],
  "suggested_crm_updates": [],
  "review_required": true
}
```

Resolve dates using call date, participant location, and actual commitments. Never fabricate an exact appointment from a vague expression. Calendar/CRM writes require the appropriate confirmed authorization and integration later; generating a draft is not the same as sending or scheduling it.

### 15.3 Evidence and factuality

Require segment evidence for claimed problems, objections, next steps, amounts, dates, competitor mentions, and promises. Validate that references exist in the finalized transcript. Keep direct quoted snippets brief and access-controlled.

Use exact-match/rule checks for numerical claims and names where practical. A summary hallucination should be flagged and corrected, not silently used to train scripts.

### 15.4 Conversation trackers

Implement two tracker types:

**Keyword:** Exact or normalized phrase matches for product names, mandated wording, and known competitors.

**Semantic:** A question with a definition, positive/negative examples, and evidence extraction. Examples: “Did the prospect state dissatisfaction with the current provider?” or “Was a specific next step agreed?” This follows the functional distinction documented by Gong.[^gong-trackers]

Version every tracker. Preview changes on a frozen evaluation set before reprocessing the entire corpus.

### 15.5 Behavioral metrics

Where timestamps and role attribution are reliable, compute talk time by side, longest monologue, question counts, turn exchanges, overlap, and response gaps. Display denominators and missing-data indicators.

Do not enforce a universal ideal talk ratio across cold calls, discovery calls, voicemail, and demos. A one-minute interruption-heavy cold call is not a 30-minute discovery meeting.

Voice intensity, pitch, or transcription sentiment must not become an employee emotion or honesty score. Evaluate observable behavior and explicitly stated content.

### 15.6 Scorecard structure

Create distinct rubrics for cold introduction, qualification, discovery, follow-up, and other genuine call types. Non-sales calls, wrong numbers, voicemail, and insufficient-evidence calls are excluded or receive a limited operational rubric.

Suggested rubric for a substantive first conversation:

| Dimension | Observable anchor examples |
|---|---|
| Identity and relevance | Correct identity; truthful, relevant purpose |
| Listening and response | Addresses what the prospect actually said; avoids repeated answered questions |
| Problem discovery | Establishes a concrete need when the conversation offers the opportunity |
| Business impact | Explores consequences without inventing urgency |
| Objection handling | Clarifies and responds using approved accurate information |
| Next step | Secures or clearly documents a suitable next action |
| Factual accuracy | No unsupported claims, guarantees, or fabricated commitments |

Use scores such as `0 = not demonstrated despite opportunity`, `1 = partial`, `2 = clearly demonstrated`, plus `N/A` and `INSUFFICIENT_EVIDENCE`. Define anchors per campaign and dimension rather than relying on generic descriptions.

Proposed overall score:

```text
100 × sum(weight_i × score_i / max_score_i) / sum(weight_i)
```

Include only applicable, sufficiently evidenced items in the denominator and display coverage. A short call with one scorable item must not appear directly comparable to a full discovery call.

Compliance concerns are separate flags, not diluted into a high average score. A rep can deliver an effective pitch and still make an impermissible claim.

### 15.7 Calibration and human review

Build a reference set of at least 100 representative reviewed conversations as a starting evaluation target, not a guarantee of statistical sufficiency. Have more than one reviewer score a subset and resolve disagreements. Track agreement, systematic bias by call type, unsupported evidence, and correction rate.

Gong's documentation itself describes human overrides and transcript-focused question design; use the same reviewability principle rather than treating AI scores as objective measurements.[^gong-scoring]

Allow reps to dispute a score and attach the relevant segment. Do not automatically use these scores for compensation, hiring, firing, or other consequential employment decisions.

### 15.8 Cross-call intelligence

Maintain an account/lead timeline of verified needs, previous objections, last commitments, and unresolved questions. Use it to modify future scripts and remind the next rep what already happened.

At team level, report objection frequency, topic-to-outcome associations, repeated factual mistakes, and missed follow-up commitments. Label associations as correlations. Do not copy top-rep phrasing into every script without checking lead mix and controlled outcomes.

Create a curated snippet/playbook library with permissioned recordings and evidence-backed examples. Human approval precedes publishing new recommended language to the entire team.

## 16. Backend services and API contracts

The following endpoints are **our application API**, not claims about Telnyx/OpenAI endpoint names.

| Endpoint | Contract |
|---|---|
| `POST /v1/auth/session` | Authenticate rep/admin; issue scoped session |
| `POST /v1/telephony/token` | Return short-lived authorized Telnyx credential token |
| `POST /v1/rep/presence` | Ready/busy/paused and device/session status |
| `POST /v1/queue/next` | Return a proposed lead packet and expiring reservation |
| `POST /v1/calls/start` | Idempotent, server-authorized start; does not trust client permission flags |
| `POST /v1/calls/{id}/hangup` | Authorized termination request |
| `POST /v1/calls/{id}/voicemail` | Check separate permission and start monitored/detached workflow |
| `POST /v1/calls/{id}/disposition` | Human outcome, callback intent, correction |
| `POST /v1/suppressions` | Immediate seller/endpoint opt-out or temporary safety hold |
| `POST /v1/leads/import` | Validate batch, create import report, preserve suppressions |
| `POST /v1/research/batches` | Schedule bounded multi-lead preparation work |
| `GET /v1/calls/{id}/analysis` | Versioned notes, evidence, scorecard, status |
| `PATCH /v1/calls/{id}/notes` | Version-aware human edit; conflict detection |
| `POST /v1/scorecards/{id}/review` | Human correction with reason/evidence |
| `POST /v1/admin/policies/{id}/publish` | Role-restricted publication with audit event |
| `POST /v1/admin/models/{id}/activate` | Validated model promotion and rollback metadata |
| `POST /webhooks/telnyx` | Signature-verified, durable, idempotent ingress |
| `GET /v1/events` or WebSocket `/v1/live` | Scoped events with cursor/reconnect support |

Example call-start input:

```json
{
  "reservation_id": "uuid",
  "rep_session_id": "uuid",
  "idempotency_key": "uuid"
}
```

The server loads the endpoint, caller ID, policy, and permissions from its own reservation. The client does not submit arbitrary trusted `from`, `to`, or `dnc_clear:true` values.

### 16.1 Event envelope

```json
{
  "event_id": "uuid",
  "sequence": 18342,
  "tenant_id": "uuid",
  "type": "coach.card_ready",
  "entity_id": "call_uuid",
  "entity_version": 27,
  "occurred_at": "2026-10-14T15:00:22Z",
  "payload": {}
}
```

Clients ignore events they have already applied and reject obsolete entity versions. Reconnection requests events after the last durable cursor. Sensitive event authorization is checked server-side.

### 16.2 Durable job queue

Use separate queue names for `call_control`, `live_coach`, `postcall`, `research`, `scripts`, and `ml`. A PostgreSQL table is enough initially. Notifications can wake workers but are not the durable queue itself.

Illustrative claim operation:

```sql
WITH candidate AS (
    SELECT id
    FROM jobs
    WHERE queue = :queue
      AND status = 'queued'
      AND available_at <= now()
    ORDER BY priority DESC, available_at, id
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
UPDATE jobs AS j
SET status = 'running',
    lease_owner = :worker_id,
    lease_expires_at = now() + interval '60 seconds',
    attempts = attempts + 1
FROM candidate
WHERE j.id = candidate.id
RETURNING j.*;
```

This is application SQL using named placeholders for the database layer. Add a suitable index and an idempotency key. Long jobs heartbeat their lease. A recovery worker handles expiration according to job type: an LLM extraction can be retried; a possibly issued phone call must be reconciled first.

### 16.3 Model-provider interfaces

```python
from typing import Protocol

class ResearchProvider(Protocol):
    async def research(self, request: dict) -> dict: ...

class ScriptProvider(Protocol):
    async def generate(self, request: dict) -> dict: ...

class LiveCoachProvider(Protocol):
    async def suggest(self, request: dict, deadline_ms: int) -> dict: ...

class PostCallProvider(Protocol):
    async def analyze(self, request: dict) -> dict: ...

class TranscriptionAdapter(Protocol):
    def normalize_event(self, provider_payload: dict) -> list[dict]: ...
```

These are architectural interfaces. Replace generic dictionaries with Pydantic request/response types in production. Each adapter must enforce deadlines, authorized model selection, privacy configuration, cost logging, schema validation, and bounded retries.

## 17. Repository layout and build sequence

```text
adaptive-dialer/
  apps/
    desktop/
      src/
        features/{dialer,lead-packet,coach,review,admin}/
        api/
        telephony/
        state/
      src-tauri/
        src/
        capabilities/
        tauri.conf.json
  services/
    backend/
      app/
        api/
        auth/
        compliance/
        telephony/
        numbers/
        scheduling/
        research/
        scripts/
        coaching/
        postcall/
        models/
        jobs/
        db/
        providers/{telnyx,openai,chatgpt_plan,groq}/
      migrations/
      tests/{unit,integration,contract,replay}/
  packages/
    contracts/
    evaluation-fixtures/
  deploy/
    compose.yml
    Caddyfile
    env.example
    backup/
    runbooks/
  docs/
    policies/
    playbooks/
    adr/
```

### 17.1 Dependencies

Use the current supported Tauri v2 toolchain, React/TypeScript, the Telnyx WebRTC package, a maintained Python runtime, FastAPI/Pydantic, an async PostgreSQL driver, SQLAlchemy/Alembic, and small CPU ML libraries. Pin tested versions in lockfiles. Verify license and ARM64 compatibility for each dependency, especially model weights and native extensions.

Keep Python speech/ML libraries off rep desktops unless a local-processing mode actually needs them. This reduces installer complexity and support burden.

### 17.2 Deployment composition

One image can provide API and worker entry points with different commands. Proposed services:

```text
caddy
postgres
api
worker-call-control
worker-live
worker-batch
worker-postcall
worker-ml (scheduled/on-demand)
```

Keep PostgreSQL on a private container network; expose only HTTPS and restricted SSH. Persist database and required object metadata outside containers. Recordings should not accumulate unbounded on the root disk.

Store secrets in protected environment/secret files outside the repository, with encryption and strict filesystem access. Rotate provider credentials and audit their use. The initial application login should use a maintained authentication implementation; do not invent password hashing or token cryptography.

### 17.3 Operational startup checklist

1. Provision the selected VM and set cloud budgets/quotas.
2. Install the supported container runtime and apply operating-system security updates.
3. Configure DNS/TLS, firewall, time synchronization, and restricted administrator access.
4. Create encrypted backups and complete a restore test before importing production leads.
5. Deploy PostgreSQL and migrations, then API/workers.
6. Create the first seller, campaign, policy version, and administrator.
7. Configure Telnyx credentials, authorized routes, webhook verification, callbacks, and spending limits.
8. Register/prepare number inventory and conduct inbound/outbound tests on owned test endpoints.
9. Onboard authorized registry data and verify freshness/coverage failure behavior.
10. Configure OpenAI and Groq adapters and hard internal job budgets.
11. Publish approved playbooks and prerecorded assets only where permitted.
12. Install signed/tested Tauri builds on pilot machines and validate audio.
13. Run controlled end-to-end test calls, reconcile billing events, and review logs.
14. Enable a small campaign only after the acceptance gates below pass.

### 17.4 Proposed configuration

```yaml
app:
  deployment_mode: single_vm
  allow_offline_outbound: false
  default_country: US

calling:
  max_live_prospects_per_rep: 1
  allow_predictive_overdial: false
  require_server_authorization: true
  preserve_lead_caller_id: true
  autonomous_screening_voice: false
  prerecorded_voicemail_default: disabled

preparation:
  leads_per_work_unit: 20
  concurrent_research_work_units: 2
  max_searches_per_company_initial: 2
  max_pages_per_company_initial: 3
  script_prefetch_count: 10
  company_research_ttl_days: 30

coaching:
  provider: groq
  model: openai/gpt-oss-20b
  alternate_model: openai/gpt-oss-120b
  max_visible_cards: 1
  max_active_requests_per_call: 1
  max_guidance_words: 25
  nonurgent_cooldown_seconds: 20
  card_ttl_seconds: 7
  end_to_end_p95_target_ms: 2500

transcription:
  provider: telnyx
  preferred_engine: Soniox
  alternative_engine: Parakeet
  require_permission: true

ml:
  baseline: pooled_logistic
  challenger: gradient_boosting
  nonurgent_exploration_fraction: 0.10
  production_promotion: manual
  optimize_unique_reach: true
  enforce_coverage_deadlines: true

security:
  llm_can_change_calling_permission: false
  export_provider_keys_to_desktop: false
  research_can_access_private_networks: false
```

These are **our application settings**, not provider API fields. Legal calling windows, recording permissions, attempt limits, and retention periods belong in reviewed, versioned campaign policies rather than a universal example file.

## 18. Privacy, security, and abuse resistance

### 18.1 Data minimization

Send only relevant call excerpts and approved context to Groq. Send only necessary transcript and lead facts to OpenAI. Do not send DNC registry datasets, unrelated CRM exports, or unnecessary sensitive identifiers to either provider.

Local hosting does not mean that data sent to an inference or transcription provider stays local. Configure provider retention controls and contractual terms appropriate to the data. Confirm availability and exceptions rather than assuming every API route has zero retention.

### 18.2 Application security

Use role-based access, tenant-aware authorization, signed updates, short-lived tokens, secure local storage, encryption at rest/in transit, and audited administrative changes. Untrusted research and transcripts never execute code.

Restrict outbound fetches, carrier routes, maximum spend, number purchases, and voicemail assets. Require extra administrative approval for changes that expand campaign permissions or geographic reach.

Avoid putting telephone numbers, tokens, transcripts, or recording URLs in ordinary operational logs. Use internal IDs and redacted samples.

### 18.3 Human control

Reps can pause dialing, override a suggested disposition, correct a transcript-derived fact, and challenge a score. They cannot override a hard opt-out or legal campaign restriction from the ordinary rep UI.

Administrators can review and correct errors with an audit trail, not silently erase compliance evidence. Model prompts and output logs are versioned so incidents can be reproduced.

## 19. Cost model

### 19.1 Current component rates

Published prices checked for this specification, not a complete quote:

| Component | Published reference |
|---|---:|
| Telnyx Voice API outbound | $0.002/minute plus destination termination |
| Example U.S. termination shown by Telnyx | $0.005/minute |
| Browser/app calling | $0.002/minute |
| Local DID | $1/month |
| Soniox transcription through Telnyx | $0.002/minute |
| Parakeet transcription through Telnyx | $0.0015/minute |
| Recording | $0.002/minute |
| Standard AMD | $0.002/invocation |
| Premium AMD | $0.0065/invocation |
| OpenAI GPT-6 Luna | $0.10 input / $0.50 output per million tokens, cited short-context tier |
| OpenAI GPT-6.1 Sol | $2 input / $10 output per million tokens, cited short-context tier |
| OpenAI web search | $10 per 1,000 tool calls, plus applicable content tokens |
| Groq GPT-OSS 20B | $0.075 input / $0.30 output per million tokens |

Sources: Telnyx, OpenAI, and Groq pricing/model documentation.[^telnyx-pricing][^openai-pricing][^groq-models]

Rates depend on service path, model/context tier, applicable fees, and future changes. Verify two-leg and dual-track billing using actual call detail records. Persistent parked agent legs can increase browser/SIP minutes substantially. Do not estimate cost from prospect talk time alone.

### 19.2 Illustrative monthly workload

The following is a worked budget, not an expected usage forecast:

| Assumed usage | Calculation | Cost |
|---|---:|---:|
| Paid 2-vCPU/4-GiB VM | Reference plan | $24.00 |
| 20 local DIDs | 20 × $1 | $20.00 |
| 5,000 prospect connected minutes | 5,000 × illustrative $0.007 combined API/termination | $35.00 |
| 6,000 separately billed browser/app minutes | 6,000 × $0.002 | $12.00 |
| 3,000 billed Soniox minutes | 3,000 × $0.002 | $6.00 |
| Research extraction: 20M input + 5M output tokens, Luna, ordinary requests | 20 × $0.10 + 5 × $0.50 | $4.50 |
| 10,000 scripts: 2,000 input + 500 output tokens each, Sol, Batch | 50% × (20 × $2 + 5 × $10) | $45.00 |
| 5,000 search tool calls | 5 × $10 | $50.00 |
| 20,000 Groq coach requests: 1,500 input + 100 output tokens each | 30 × $0.075 + 2 × $0.30 | $2.85 |
| Post-call jobs: 3M input + 0.6M output tokens, Sol, Batch | 50% × (3 × $2 + 0.6 × $10) | $6.00 |
| **Illustrative subtotal** | Before the exclusions below | **$205.35** |

The token counts are stipulated billed quantities for this example. Include reasoning tokens, retries, cache behavior, and search-content tokens as actually reported. The example is not a claim that all stages fit those token counts or that every job belongs in delayed Batch.

Potential additions: dual-track transcription billing, immediate post-call requests, recordings, number lookup, AMD, backups, domain, taxes, carrier surcharges, DNC subscriptions, number-reputation service, and engineering/operations.

Examples: 3,000 recording minutes add $6; 10,000 premium-AMD invocations add $65. Use actual invoked/billed units, not all attempted dials automatically.

On a qualifying Oracle free VM, subtract the $24 hosting assumption. Cheaper script models can reduce script-generation cost, but only after quality evaluation. Search caching and avoiding unnecessary AMD often matter more than shaving a few text tokens.

### 19.3 DNC budget is separate

At the cited fee schedule, a covered seller accessing 30 area codes would have an illustrative annual registry fee of `(30 − 5) × $85 = $2,125`, approximately $177.08/month when amortized. Applicable exemptions and exact obligations need review; state data and services can add costs.[^dnc-fees]

Do not infer that five free area codes for your outbound DIDs cover prospects whose telephone numbers span many other area codes. Registry access coverage concerns the prospect numbers being checked/called.

### 19.4 Cost-control implementation

Track usage per provider, tenant, campaign, work unit, lead, call, and model version. Enforce internal daily/monthly limits before starting optional research or analysis. Provider dashboards are a secondary check, not the only control.

Limit searches, page fetches, retries, generated tokens where supported, AMD invocations, storage retention, and number procurement. For ChatGPT-plan requests that do not support the same token parameters, enforce external concurrency/job/deadline limits and handle plan exhaustion explicitly.

The cheapest useful system uses hosted text inference economically while spending engineering effort on data quality and reliability. It does not pretend telephony, authorized DNC access, and operations are free.

## 20. Implementation phases and acceptance gates

### Phase A: Telephony/platform proof

Deliver a minimal Tauri app, scoped login/token issuance, one server-authorized call, callbacks, verified webhooks, and durable call records.

**Gate:** Actual Windows/macOS/Linux targets that will be supported pass microphone, audio, NAT, reconnect, sleep/wake, and controlled long-call tests. Confirm the carrier traffic pattern and billing path. Do not build the entire AI layer before this gate.

### Phase B: Safe shared operations

Implement seller isolation, endpoint validation, DNC imports, immediate suppressions, pre-call authorization, shared regional pools, stable bindings, reservations, and basic coverage scheduling.

**Gate:** Concurrent rep actions cannot double-dial a lead or bypass policy; stale registry data cannot produce a false clearance; backup restoration does not resurrect an opt-out.

### Phase C: Batch preparation

Add bounded research, provenance, company caching, script writer/modifier, approved playbooks, script-prefetch UI, and cost limits. Implement the metered OpenAI adapter first; add the official plan-usage adapter only under its eligibility rules.

**Gate:** Every personalized factual statement can be traced to appropriate evidence; wrong-company matches and prompt injection fixtures fail safely; partial batches resume without duplicating work.

### Phase D: Speech and post-call intelligence

Add permitted transcription, transcript normalization, evidence-linked notes, manual corrections, trackers, and calibrated scorecards.

**Gate:** Reviewers can inspect evidence and correct results; short/irrelevant calls are not unfairly scored; late transcript revisions preserve human edits.

### Phase E: Live guidance

Add state-based triggers, approved retrieval, Groq tiny-card generation, TTL/cooldown logic, fallback cards, and replay evaluation.

**Gate:** Latency targets are measured; stale cards are discarded; no AI outage disrupts audio; controlled trials show useful assistance without unacceptable distraction.

### Phase F: Adaptive ML and number-health learning

Add feature snapshots, pooled statistical models, gradient-boosting challenger, exploration logging, delayed outcomes, and production model approval.

**Gate:** Improved original-cohort outcomes without worse coverage, policy violations, or hidden lead exclusion. Keep a baseline control and rollback.

### Phase G: Permissioned detached voicemail and advanced capabilities

Add independently controlled voicemail legs, opt-out mechanism where required, watchdogs, recovery behavior, and adaptive voicemail experiments. Then evaluate cross-call semantic search, richer integrations, and optional acoustic/multimodal features for concrete permitted tasks.

**Gate:** The exact mailbox/bridge/playback behavior passes controlled tests, including false classifications and backend failure. No feature is enabled merely because its API call returned HTTP 200.

## 21. Minimum test matrix

| Test category | Required cases |
|---|---|
| Call permissions | Internal opt-out, stale/missing registry coverage, revoked consent, uncertain timezone, policy change after queueing |
| Concurrency | Two reps request same lead; same endpoint on duplicate leads; expired reservation during an active call |
| Carrier events | Duplicate, out-of-order, missing/delayed webhook; timeout after a possibly successful dial |
| Human answers | Immediate greeting, gatekeeper, wrong party, long silence, accented speech, background noise |
| Screening/IVR | Human rep answers screening; machine result later changes; switchboard/extension navigation |
| Voicemail | Greeting not finished, real beep, no beep, full mailbox, false machine result, late human, playback failure |
| Recovery | Desktop crash, VM restart, lost database connection, provider outage, backup restore, queued old command |
| Coaching | Stale suggestion, contradicted transcript, model refusal, rate limit, prompt injection, hallucinated claim |
| Scoring | Short call, interrupted call, wrong call type, N/A dimension, conflicting reviewers, rep appeal |
| Research | Wrong company, unsupported SEO claim, stale page, duplicate domain, malicious URL, token/search budget exhausted |
| Pooling | Caller ID remains stable, unhealthy DID paused, callback ambiguity, different sellers isolated |
| Security | Desktop cannot access master keys or initiate unauthorized carrier routes; tenant data cannot cross accounts |
| Billing | Compare measured minute/track/leg/invocation counts with provider records |

Use owned/permissioned test endpoints and synthetic fixtures before production calls. Contract tests should track provider schema changes without placing unintended real calls.

## 22. Launch metrics and operating rules

### 22.1 Metrics that matter

Measure human pickup, right-party connection, unique lead reach within the campaign horizon, held meetings per original eligible lead, held meetings per rep-hour, coverage overdue, opt-outs, falsely classified humans, and cost per useful outcome.

Keep a stable denominator including unsuccessful actual attempts. Report imported-but-suppressed leads separately with reasons; do not disguise necessary suppression as optimization.

### 22.2 Health and service objectives

Monitor webhook persistence latency, command queue age, call-control errors, answer-to-rep audio timing, transcription delay, coaching usefulness/latency, model failures, database load, disk growth, budget consumption, and registry freshness.

A normal business dashboard can lag; opt-out propagation and call authorization cannot. Prioritize accordingly.

### 22.3 Definition of success

The system succeeds when it reaches and converts more of the original eligible list with accountable costs and acceptable customer/rep experience. A 15% pickup target remains a campaign outcome to earn and verify, not a guarantee produced by choosing a particular model or number pool.

The first production release should already contain safe calling, shared state, evidence-backed scripts, callbacks, and clean measurement. Advanced scoring, live guidance, and adaptive policies then improve a system whose underlying events can be trusted.

## 23. Decisions that require account-level validation before code is finalized

These are bounded integration gates, not reasons to postpone the overall architecture:

- Telnyx approval for the proposed traffic pattern, caller-ID inventory, and exact billing topology.
- Telnyx WebRTC behavior in each supported Tauri webview and headset configuration.
- STT engine track attribution, finality, timestamps, latency, and dual-track billing.
- Bridge leg survival and voicemail playback behavior on the actual call path.
- OpenAI model/tool availability and eligibility for the optional ChatGPT-plan route.
- Groq production quotas and measured end-to-end card latency.
- Seller-specific DNC access, state rules, recording/transcription permissions, and prerecorded-message permissions.
- VM capacity, backup/restore recovery, and ARM64 dependency support if using Oracle A1.

## 24. Source register

All sources below were consulted for the October 14, 2026 specification. Vendor capability and pricing pages document published offerings, not independently verified performance. Proposed architecture, thresholds, schemas, and acceptance tests are this specification's design choices.

[^gong-scoring]: Gong, [Gong AI for scoring](https://help.gong.io/v1/docs/gong-ai-for-scoring), and [Create and manage scorecards](https://help.gong.io/docs/create-and-manage-scorecards).
[^gong-trackers]: Gong, [Create AI trackers](https://help.gong.io/docs/create-ai-trackers).
[^rpc-paper]: Bollapragada and Nair, [Improving Right Party Contact Rates at Outbound Call Centers](https://onlinelibrary.wiley.com/doi/abs/10.1111/j.1937-5956.2010.01156.x), *Production and Operations Management*, 2010.
[^tabpfn-paper]: Hollmann et al., [Accurate predictions on small data with a tabular foundation model](https://www.nature.com/articles/s41586-024-08328-6), *Nature*, 2025.
[^tabpfn-repo]: Prior Labs, [TabPFN repository, model and licensing documentation](https://github.com/PriorLabs/tabpfn).
[^eu-emotion]: European Commission AI Act Service Desk, [Article 5: Prohibited AI practices](https://ai-act-service-desk.ec.europa.eu/en/ai-act/article-5).
[^oracle]: Oracle, [Always Free Resources](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm).
[^do]: DigitalOcean, [Droplet pricing](https://www.digitalocean.com/pricing/droplets).
[^telnyx-contact-center]: Telnyx, [WebRTC contact center use case](https://developers.telnyx.com/docs/voice/webrtc/use-cases/contact-center).
[^tauri-webviews]: Tauri, [Webview versions](https://v2.tauri.app/reference/webview-versions/).
[^tauri-capabilities]: Tauri, [Capabilities](https://v2.tauri.app/security/capabilities/).
[^libphonenumber]: Google, [libphonenumber FAQ](https://github.com/google/libphonenumber/blob/master/FAQ.md).
[^telnyx-lookup]: Telnyx, [Number Lookup quickstart](https://developers.telnyx.com/docs/identity/number-lookup/quickstart).
[^ftc-guide]: Federal Trade Commission, [Complying with the Telemarketing Sales Rule](https://www.ftc.gov/business-guidance/resources/complying-telemarketing-sales-rule). Use the current regulation for controlling details where a guide is outdated.
[^dnc-fees]: eCFR, [16 CFR 310.8: Fee for access to the National Do Not Call Registry](https://www.ecfr.gov/current/title-16/chapter-I/subchapter-C/part-310/section-310.8).
[^rnd]: Reassigned Numbers Database, [Querying data](https://www.reassigned.us/querying-data).
[^tsr-abusive]: eCFR, [16 CFR 310.4: Abusive telemarketing acts or practices](https://www.ecfr.gov/current/title-16/chapter-I/subchapter-C/part-310/section-310.4).
[^fcc-rules]: eCFR, [47 CFR 64.1200: Delivery restrictions](https://www.ecfr.gov/current/title-47/chapter-I/subchapter-B/part-64/subpart-L/section-64.1200).
[^recordkeeping]: eCFR, [16 CFR 310.5: Recordkeeping requirements](https://www.ecfr.gov/current/title-16/chapter-I/subchapter-C/part-310/section-310.5).
[^siwc-overview]: OpenAI, [ChatGPT plan usage overview](https://developers.openai.com/siwc/token-sharing-open-source).
[^siwc-vm]: OpenAI, [ChatGPT plan usage on self-hosted VMs](https://developers.openai.com/siwc/token-sharing-open-source/self-hosted-vms).
[^siwc-inference]: OpenAI, [ChatGPT plan usage: models and inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference).
[^siwc-limits]: OpenAI, [ChatGPT plan usage preview limitations](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations).
[^openai-websearch]: OpenAI, [Web search guide](https://developers.openai.com/api/docs/guides/tools-web-search).
[^openai-pricing]: OpenAI, [API pricing](https://developers.openai.com/api/docs/pricing).
[^openai-batch]: OpenAI, [Batch API guide](https://developers.openai.com/api/docs/guides/batch).
[^fcr]: Free Caller Registry, [Business number registration and limitations](https://www.freecallerregistry.com/fcr/).
[^stir]: Telnyx, [STIR/SHAKEN attestation behavior](https://developers.telnyx.com/docs/voice/stir-shaken/attestation-behavior).
[^telnyx-reputation]: Telnyx, [Number Reputation pricing](https://developers.telnyx.com/docs/number-reputation/pricing).
[^vw]: Vowpal Wabbit, [Contextual Bandits tutorial](https://vowpalwabbit.org/docs/vowpal_wabbit/python/latest/tutorials/python_Contextual_bandits_and_Vowpal_Wabbit.html).
[^telnyx-amd]: Telnyx, [Answering Machine Detection](https://developers.telnyx.com/docs/voice/programmable-voice/answering-machine-detection).
[^telnyx-webhooks]: Telnyx, [Receiving webhooks](https://developers.telnyx.com/docs/voice/programmable-voice/receiving-webhooks).
[^telnyx-short]: Telnyx, [Short-duration calls](https://support.telnyx.com/en/articles/1130707-what-are-short-duration-calls).
[^telnyx-bridge]: Telnyx, [Bridge calls](https://developers.telnyx.com/api-reference/call-commands/bridge-calls).
[^telnyx-playback]: Telnyx, [Play audio URL](https://developers.telnyx.com/api-reference/call-commands/play-audio-url).
[^telnyx-stt]: Telnyx, [Speech-to-text guide](https://developers.telnyx.com/docs/voice/programmable-voice/speech-to-text).
[^telnyx-transcription-api]: Telnyx, [Transcription start API](https://developers.telnyx.com/api-reference/call-commands/transcription-start).
[^telnyx-pricing]: Telnyx, [Voice API pricing](https://telnyx.com/pricing/voice-api).
[^groq-models]: Groq, [Supported models and rates](https://console.groq.com/docs/models).
[^groq-structured]: Groq, [Structured outputs](https://console.groq.com/docs/structured-outputs).

[^connectrate]: ConnectRate, [Public feature descriptions](https://connectrate.ai/).
[^telnyx-dial]: Telnyx, [Dial API and linked-leg bridge settings](https://developers.telnyx.com/api-reference/call-commands/dial).
