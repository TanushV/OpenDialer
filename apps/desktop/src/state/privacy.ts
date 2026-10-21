// Only derived, nonverbatim state may enter a durable desktop cache. The cache is
// an allowlist: a new field persists only after it is reviewed and listed here.
const PERSISTED = new Set(
  `
  action active active_playbook agent_call_control_id all_attempt_rate
  analysis_incomplete applies approach approved approved_assets as_of at
  attempt_count attempts audio_ready author_id authorization_expires_at
  authorized billing_verified bins blocked_count blockers blocks bootstrap
  bound_number_id brier calibration calibration_cutoff call call_id
  call_memory call_type callback_at callback_due_at callback_evidence_current
  callback_note callback_phone callback_proof callback_tested calls
  calls_omitted calls_total campaign campaign_id can_disconnect capabilities
  capabilities_as_of capabilities_stale capacity cards cell_known
  cell_successes cell_unknown checks_scope checksum claims cleared_count
  closing_question coach_preferences coaching_points code comments commitments
  commitments_omitted commitments_total company comparison concept
  conditioning_excluded confidence_high confidence_low configuration_status
  configured confirmation_state connect_rate connection_id contact_role
  context_coverage conversation_ablation conversation_state
  conversion_given_right_party conversion_goal conversion_samples
  conversions_per_rep_hour correction_reason counts_complete coverage
  coverage_due_at coverage_overdue created_at daily_limit date decision_id
  destination detached detached_topology_verified dialable_queue_count digits
  dimension discovery_questions disposition_input domain due_at
  eligible_for_serving eligible_now end_ms ended_at error estimate_basis
  estimate_level estimate_policy estimate_policy_version evaluation_cutoff
  event_cursor evidence evidence_at evidence_status expected_calibration_error
  expires_at explanation external_reputation fact_id facts fallback
  feature_schema_version filter final_voicemail_asset fresh freshness
  gatekeeper goal goal_prior grounding_status groups health held held_meetings
  human_notes human_prior human_probability id idempotency_key
  identity_configured inbound inbound_state inspected_count integrity_ok
  interpretation_coverage interpretation_gaps interpreted_segments interval
  interval_kind invitation_sent is_final items jobs jurisdiction kind
  last_outcome late_human_recovery_destination lead lead_company lead_id
  lead_name lead_timezone leads lifecycle local_end_hour local_start_hour
  local_time lower max_attempts mean_prediction meeting memory_omitted_items
  metrics mode models muted name needs needs_time_confirmation negative
  next_cursor next_question note notes notes_version npas number number_id
  numbers objection_cards objections objective_stage observations observed_at
  observed_interval observed_rate omitted_count open_questions opener
  original_cohort outbound_ready outcome outcome_goal outcome_window_days
  outreach_hold outside_known overall owner_id ownership_reference pages
  pending_count pending_segments period permissions phone pinned
  playbook_version playbooks policy policy_id pooled_stages positive
  preparation preparation_revision presence previous_calls prior_answers
  prior_only problems product_description prohibited_claims projection proof
  prospect_brief provider provider_id provider_present published purpose query
  queue quiescence rate raw_evidence_status readiness ready_for_rep
  ready_to_call reason reason_codes reasons recording_available
  recovery_reason reference_rate refresh_error refresh_status refreshed_at
  region registries registry_required remedy rep_id rep_work_seconds research
  reservation_id reserved_cost_usd resolution_id retained_notes retrieved_at
  review_receipt_id review_reference review_required review_status
  review_token reviewable reviewed_scorecard reviewer_id reviewer_role reviews
  revision right_party_given_human right_party_unique right_prior role
  rolling_evaluations route routed_rep_id routing_attempts rubric_revision
  rubric_snapshot rubric_version rundown_points rundown_status samples scanned
  score scorecard scorecard_id scorecard_version screening screening_intro
  script script_version search_complete seen_during_training segment_id seller
  seller_id shrinkage side simulated simulation snoozed_until source
  source_conflicts source_id source_observation_id source_retracted
  source_stale source_url source_version sources spam_label stage
  stage_support stages start_ms started_at state state_registry_required
  state_version statement statistical_guidance status subgroups
  subscription_id subscription_reference suggested_outcome suggested_update
  summary support suppressed synthetic tags task_id task_owners tasks
  tasks_omitted tasks_total template_version tenant_id text time_window
  timezone timezone_confidence title topology_test_reference total total_count
  total_segments tracker_definitions_omitted tracker_id trackers
  training_cutoff transcript_version trial_active trial_ends_at
  unanswered_questions unavailable_segments uncertainty unknown unknowns
  unprocessed_speech_events unresolved upper url user user_id version
  voicemail_enabled voicemail_optout_supported warnings why_now window_days
  work_assumption_seconds work_prior work_time
`.split(/\s+/),
);
// Containers whose child keys are data (stage names, metric names, sections).
const RECORDS: Record<string, number> = {
  metrics: 1,
  pages: 1,
  stage_support: 1,
  stages: 1,
  support: 1,
  subgroups: 2,
};
export function persistentCache<T>(value: T): T {
  const clean = (v: unknown, freeKeys = 0): unknown => {
    if (Array.isArray(v)) return v.map((item) => clean(item));
    if (!v || typeof v !== "object") return v;
    const result: Record<string, unknown> = {};
    for (const [key, item] of Object.entries(v)) {
      if (key === "transcript" || key === "transcript_revisions")
        result[key] = [];
      else if (key === "raw_evidence_status") result[key] = "expired";
      else if (freeKeys > 0) result[key] = clean(item, freeKeys - 1);
      else if (PERSISTED.has(key)) result[key] = clean(item, RECORDS[key] ?? 0);
    }
    return result;
  };
  return clean(value) as T;
}

/** A saved keypad press may replay only briefly; its digits never outlive that. */
export const KEYPAD_REPLAY_MS = 10_000;
export function boundKeypad<T extends { created_at: number; digits: string }>(
  value: T | null,
  now = Date.now(),
): T | null {
  if (!value) return null;
  const age = now - value.created_at;
  if (!Number.isFinite(age) || age < 0 || age >= 300_000) return null;
  return age < KEYPAD_REPLAY_MS ? value : { ...value, digits: "" };
}

type Temporary = {
  raw_remaining_ms?: number;
  remaining_ms?: number;
  _expires?: number;
  _monoExpires?: number;
};
type Speech = {
  transcript?: Temporary[];
  coach?: Temporary | null;
  raw_evidence_status?: string;
};
/** First-seen deadlines by segment revision: a refresh may shorten a countdown, never restart it. */
const deadlines = new Map<string, { wall: number; mono: number }>();

export function receiveSpeech<T extends Speech>(
  value: T,
  now = Date.now(),
  mono = performance.now(),
): T {
  for (const [key, seen] of deadlines)
    if (seen.wall <= now || seen.mono <= mono) deadlines.delete(key);
  const stamp = <
    V extends Temporary & { segment_id?: string; revision?: number },
  >(
    v: V,
    remaining: number | undefined,
  ): V => {
    const ttl = Math.min(
      300000,
      Math.max(0, Number.isFinite(remaining) ? remaining! : 0),
    );
    let wall = now + ttl;
    let monoEnd = mono + ttl;
    if (v.segment_id) {
      const key = `${v.segment_id}:${v.revision ?? ""}`;
      const seen = deadlines.get(key);
      if (seen) {
        wall = Math.min(wall, seen.wall);
        monoEnd = Math.min(monoEnd, seen.mono);
      }
      deadlines.set(key, { wall, mono: monoEnd });
    }
    return { ...v, _expires: wall, _monoExpires: monoEnd };
  };
  return {
    ...value,
    ...(value.transcript
      ? {
          transcript: value.transcript.map((s) => stamp(s, s.raw_remaining_ms)),
        }
      : {}),
    ...(value.coach
      ? { coach: stamp(value.coach, value.coach.remaining_ms) }
      : {}),
  };
}
export function expireSpeech<T extends Speech>(
  value: T,
  now = Date.now(),
  mono = performance.now(),
): T {
  const live = (v: Temporary) =>
    (v._expires ?? 0) > now && (v._monoExpires ?? 0) > mono;
  const transcript = value.transcript?.filter(live);
  const coach = value.coach && live(value.coach) ? value.coach : null;
  if (
    transcript?.length === value.transcript?.length &&
    coach === (value.coach ?? null)
  )
    return value;
  return {
    ...value,
    ...(transcript ? { transcript } : {}),
    coach,
    ...(value.transcript?.length && !transcript?.length
      ? { raw_evidence_status: "expired" }
      : {}),
  };
}
export function speechDeadline(value: Speech): number | null {
  const ends = [
    ...(value.transcript ?? []),
    ...(value.coach ? [value.coach] : []),
  ].map((v) =>
    Math.min(
      (v._expires ?? 0) - Date.now(),
      (v._monoExpires ?? 0) - performance.now(),
    ),
  );
  return ends.length ? Math.max(0, Math.min(...ends)) : null;
}
