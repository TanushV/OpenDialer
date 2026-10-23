# Authority, durable effects, and local execution

The desktop uses React/TypeScript in Tauri v2. Narrow Rust commands launch the frozen local authority, run the optional SSH webhook forward, open HTTPS sources outside the privileged webview, and install signature-verified updates. Native capabilities expose no generic frontend filesystem, shell or provider-key access. Media flows between the rep, Telnyx and the prospect.

SQLite is the local authority; PostgreSQL is the shared authority. Both use identical domain services over plain JSON documents with indexed columns for the busy tables. UUID object identity, UTC events, immutable policy/script versions, database unique claims and audit history retain provenance. The schema is created on a fresh store at startup; there is no upgrade path from 0.2/0.3 stores.

An authority transaction reserves lead, endpoint, rep and number capacity and creates a command intent. An endpoint/seller fence orders concurrent opt-outs and starts. The call-control lane rechecks current permission and commits submission state before any external request; no database transaction stays open during network I/O. Deterministic command IDs and provider receipts reconcile uncertainty. A lost dial response never becomes a new random call. Carrier-side duration limits and a recovery sweep bound orphan obligations.

Jobs use leases, heartbeats, retries and dead-letter states. Priority lanes separate call control, live coaching, research/scripts, postcall and ML. PostgreSQL claims use SKIP LOCKED; local SQLite serializes short writes. The small-server target does not need Kafka, Kubernetes, a vector database or a self-hosted LLM.

Registry datasets are exact seller-authorized compressed bitmaps. Internal suppressions, known endpoint problems, registry freshness/coverage, explicit timezones, cadence, budget, routes, rep audio and approved caller identity are checked independently. Registry membership never enters model features. Restores keep outbound paused until every restored live call is reconciled with the carrier.

Research fetches are limited to the imported company domain, with bounded redirects, content types, sizes and time. Retrieved text never executes instructions. Approved fallback scripts remain available without a model. Model-backed outputs retain source IDs, transcript evidence, prompt/model versions and native usage; absent provider bills are never called verified spend.

Scheduling prioritizes callbacks and coverage before probability. Pooled estimates separate human answer, right party and next step. A checksum-verified locally trained calibrated challenger can influence non-control ranking only after manual calibration/online/coverage/policy gates. Approved variants have exact action propensities; a stable control remains. No caller-ID churn or opt-out experiments are allowed.

Detached voicemail is disabled by default. Actual mailbox readiness, separate prerecorded permission, approved media, digit-9 opt-out support, a verified surviving leg and a real late-human destination are required. Rep claims release only after playback-start confirmation; independent number leases and watchdogs retain the old call. No playback event is labeled proof that a mailbox saved a message.
