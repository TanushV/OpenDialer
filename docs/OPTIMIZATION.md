# Pickup, conversion and grounded guidance

The October 16 extension is tracked in [OPTIMIZATION_PLAN.md](OPTIMIZATION_PLAN.md). It optimizes observed conversions per original eligible lead and per rep-hour. Human pickup, right-party reach, qualified conversations, held meetings and won sales remain separate. The default goal is a held meeting. A won sale does not imply that a meeting occurred.

## Configure and operate

In Intelligence, select the conversion goal and delayed outcome window, reviewed exploration share, rep capacity, planning horizon and per-number ceiling. Save settings, then refresh learning/capacity. Goal/window changes begin a new cohort; the history selector retains earlier cohorts. Future opportunities respect retry policy, local windows, requested callbacks, attempt limits and coverage. A visible shortfall means the configured workload exceeds capacity.

Numbers shows measured first-party delivery/pickup evidence, case-mix adjustment, sample counts, uncertainty, warmup limits and active obligations. Unknown health stays unknown. Existing stable bindings are preferred, then permitted regional/general inventory. Paused numbers retain inbound callbacks. Owned numbers awaiting setup or review appear separately and reduce procurement proposals without being counted as callable capacity. Review legitimate use before increasing bounded workload. Purchase proposals still require fresh carrier quotes, explicit approved budgets and durable reconciliation of uncertain orders.

Record delayed qualified/meeting/sale results in the call review with a source reference. Correct mistakes using the audited retraction control. Open outcome windows stay pending; retractions cannot remove opt-outs. Reports retain imported/excluded/originally eligible, uncalled, converted and matured denominators.

## Learning and controlled evaluation

Every new attempt links a frozen pre-call feature snapshot, predictions, allowed treatments, actual selection probability, allocation reason and submission authorization. Older attempts without snapshots remain in history and are excluded from fabricated training data. Registry membership, sensitive inference, transcripts and future results are not prediction inputs.

Training uses authority-owned encrypted operational snapshots only. It runs in the bounded ML lane, independently of calling controls. Human/right-party/conversion stages use independent chronological lead/endpoint partitions and separate calibration; pending conversions do not become negative labels. Models retain held-out Brier/log loss, pooled-baseline comparison, calibration bins and subgroup sample/seen-group boundaries. Sparse stages retain the pooled baseline.

A reviewed bounded model pilot can serve enrolled candidate leads while a stable control keeps baseline behavior. It expires automatically; its maximum enrollment and duration are explicit. Promotion needs a saved live report for that exact model, observed model exposure, closed windows, sample sufficiency, no unknown/failed policy evidence, preserved coverage and explicit review. Tiny pilots cannot prove ordinary conversion lift. Baseline restoration is immediate and audited; rollback to another model requires prior activation for the current goal.

Optional number comparisons assign fixed region clusters, preserving all existing bindings. Their control prefers an owned general number and their candidate prefers owned regional inventory; neither expands eligibility. Cluster-level summaries are required for inference. Optional coaching comparisons randomize two-lead blocks within each rep before treatment, comparing approved static guidance with the configured coach. Reports retain rep strata, unassigned population, pending outcomes and reviewed usefulness/grounding/timing/distraction. They do not activate a model or assert causal lift automatically.

## Cerebras through OpenRouter

In Settings → Providers, select OpenRouter and enter an OpenRouter key on the authority. The default model is openai/gpt-oss-120b. Requests pin Cerebras, disable provider fallback, require supported structured outputs, bound output/deadline and reject a response without a matching provider receipt. Groq remains an explicit compatibility route; approved static help works without a model key. Research/script/post-call provider settings are independent.

The coach receives approved formulations, current versioned state, exact stable transcript evidence, fresh scoped lead facts and bounded earlier-call context. Local retrieval chooses a relevant fallback; the text model can choose among a bounded approved catalog. Spoken suggestions must match reviewed wording. Quoted semantic needs/impact/objections/questions/signals/commitments survive only while their source revision remains valid. Refusals, corrections, cooldown, expiry, obsolete requests and opt-outs suppress stale guidance. Audio, hangup and calling authorization never depend on a model response.

Scoped authenticated live events provide reconnect/cursor replay without ordinary active-call polling. Feedback records only the dimensions the rep actually reviewed. State, retrieval, inference, event-to-render and render times have separate boundaries. STT and complete utterance-to-card latency stay unknown without reliable provider timing/live measurement; the 2.5-second p95 objective is not a demonstrated result.

Reviewed replay cases can be evaluated from Intelligence or with `uv run python scripts/evaluate_coach.py --output work/coach-report.json`. The command defaults to OpenRouter/Cerebras and reads OPENROUTER_API_KEY from the protected environment; Groq is an explicit option. Replays validate approved meaning, evidence, expected action/card and deadlines, and preserve per-attempt provider/usage receipts. Synthetic replay quality is separate from real conversation quality and business outcome lift.

## Modes, updates and evidence

The same services run on local SQLite and connected PostgreSQL. Local mode has no shared-server dependency; configured Telnyx/inference calls still contact their respective providers. Connected mode centralizes authorization, data and pooled learning. Call audio remains desktop ↔ Telnyx ↔ prospect. Use HTTPS near the operators/providers and measure latency under actual load.

Updates preserve application identity, IDs, human notes, suppressions and protected sessions. Use the signed in-app updater on Mac and Windows; never uninstall/reset to update. This preview has integrity signatures, but public publisher trust/notarization is a separate gate. Account onboarding, live carrier/media behavior, funded inference, representative replay labels and statistically sufficient campaign lift remain explicitly unverified without those resources.
