import { TERMINAL_STATES } from "./state/callStates";

export interface Session {
  access_token: string;
  tenant_id: string;
  seller_id: string;
  user_id: string;
  name: string;
}
export interface Runtime {
  url: string;
  local_token: string;
  simulation: boolean;
}
export interface Lead {
  id: string;
  version: number;
  name: string;
  phone: string;
  company: string;
  domain: string;
  campaign_id: string;
  timezone: string | null;
  timezone_confidence: string;
  region: string;
  state?: string | null;
  state_source?: string;
  list_name?: string;
  list_names?: string[];
  dial_paused?: boolean;
  archived?: boolean;
  status: string;
  suppressed: boolean;
  coverage_due_at: string;
  attempt_count: number;
  callback_due_at?: string;
  outreach_hold?: string | null;
  source?: "csv" | "crm";
  live_status?: string | null;
  number_status?: string;
  number_label?: string;
  number_hold?: string | null;
  number_hold_reason?: string | null;
  dial_by_hand?: boolean;
  trigger?: Trigger | null;
  trigger_label?: string;
  bridge?: { text: string; is_inference: true } | null;
}
export interface Trigger {
  type: string;
  date: string | null;
  source_url: string | null;
  quote: string | null;
  evidence: "page" | "row_only" | null;
  strength: "A" | "B" | "C" | "none";
  from_row: boolean;
}
export interface FollowupTask {
  lead_name?: string;
  lead_company?: string;
  id: string;
  version: number;
  inbound?: boolean;
  inbound_state?: string;
  routed_rep_id?: string | null;
  routing_attempts?: {
    id: string;
    rep_id?: string | null;
    provider_id?: string | null;
    state: string;
  }[];
  callback_phone?: string | null;
  route?: string;
  recovery_reason?: string;
  call_id?: string | null;
  lead_id: string;
  owner_id?: string | null;
  action: string;
  due_at: string | null;
  timezone?: string | null;
  status: string;
  source?: string;
  source_stale?: boolean;
  source_retracted?: boolean;
  confirmation_state?: "suggested" | "confirmed" | "needs_review";
  snoozed_until?: string | null;
  source_conflicts?: string[];
  retained_notes?: { task_id: string; text: string }[];
  review_required?: boolean;
  kind?: string;
  /** An unconfirmed AI-suggested callback holds outreach only until then. */
  callback_suggestion_until?: string | null;
  time_window?: {
    date: string;
    period: string | null;
    timezone: string | null;
    local_time?: string;
  } | null;
}
export interface NumberRecord {
  /** Outbound caller-name listing sent to Telnyx and what it read back. */
  caller_name?: {
    requested: string;
    listed: string | null;
    status: string;
    at: string;
  };
  id: string;
  version: number;
  outbound_ready?: boolean;
  workspace_test_passed?: boolean;
  last_sip_block_at?: string;
  recent_sip_blocks?: number;
  reason?: string;
  today_limit?: number;
  synthetic?: boolean;
  external_reputation?: {
    source: string;
    observed_at: string;
    expires_at: string;
    ownership_reference: string;
    spam_label: string | null;
  };
  phone: string;
  region: string;
  lifecycle: string;
  daily_limit: number;
  capacity: number;
  identity_configured: boolean;
  provider_id?: string;
  provider_present?: boolean;
  connection_id?: string;
  configuration_status?: string;
}
export interface Segment {
  raw_remaining_ms?: number;
  _expires?: number;
  _monoExpires?: number;
  segment_id: string;
  revision: number;
  side: string;
  text: string;
  is_final: boolean;
  start_ms: number | null;
  end_ms: number | null;
}
/** R10.1: one live tip or private answer, kept in memory for its short life. */
export interface Coach {
  _expires?: number;
  _monoExpires?: number;
  id: string;
  kind: "TIP" | "ANSWER";
  say_or_ask: string;
  private: boolean;
  status: "streaming" | "ready";
  at?: string;
  expires_at?: string;
  remaining_ms?: number;
}
export interface Call {
  /** R10.2: the one-request after-call summary, once written. */
  aftercall?: {
    status: "ready" | "failed" | "no_speech" | "unavailable";
    summary?: string | null;
    next_step?: string | null;
    error?: string;
  } | null;
  /** Carrier hang-up side: "callee" is the prospect, "caller" is this app's side. */
  provider_hangup_source?: string | null;
  connected_seconds?: number | null;
  /** Telnyx found a mailbox on the final attempt: the line stays open for the rep's message. */
  voicemail_turn?: boolean;
  stay_on_voicemail?: boolean;
  machine_detected?: boolean;
  transcription_source?: string;
  transcription_layout?: string;
  transcription_disabled?: boolean;
  agent_provider_id?: string;
  /** Rep-visible edit counter used for wrap-up conflicts (row version moves on bookkeeping). */
  edit_version?: number;
  disposition_input?: {
    outcome: string;
    contact_role: string;
    callback_at: string | null;
    callback_note: string;
    audio_result?: "unknown" | "two_way" | "problem";
    screened?: boolean;
    delivery_test?: boolean;
  };
  rundown_status?: "pending" | "partial" | "ready" | "unavailable";
  /** Set on a saved outcome whose callback time was not applied to a task. */
  callback_review?: {
    status: "needs_review";
    reason: string;
    task_ids: string[];
  } | null;
  canonical_task_id?: string | null;
  outcome_source?: string;
  suggested_outcome?: {
    code: string;
    contact_role: "right_party" | "gatekeeper" | "wrong_party" | "unknown";
    reason?: string;
    source?: string;
  } | null;
  lead_name?: string;
  lead_company?: string;
  version: number;
  raw_evidence_status?: string;
  coach_preferences?: { muted?: boolean };
  /** Frozen at dial time; voicemail is offered only on the final permitted attempt. */
  attempt_ordinal?: number;
  attempt_allowance?: number;
  id: string;
  rep_id: string;
  lead_id: string;
  number_id: string;
  reservation_id: string;
  state: string;
  started_at: string;
  ended_at?: string;
  outcome: string | null;
  transcript_version: number;
  provider_id: string | null;
  transcript?: Segment[];
  coach?: Coach | null;
  /** Why the live helper is quiet, when it is: no key, budget or a route failure. */
  helper_status?: {
    status: "available" | "paused";
    reason?:
      | "not_configured"
      | "budget"
      | "transport_fault"
      | "provider_unverified"
      | "provider_mismatch"
      | null;
  } | null;
}
export interface ScriptOption {
  notes?: string;
  id: string;
  title: string | null;
  text: string;
  picked?: boolean;
  rank?: number | null;
}
export interface ScriptSection {
  key?: string;
  title: string;
  /** Older call snapshots carry plain text instead of options. */
  text?: string;
  options?: ScriptOption[];
}
export interface ScriptTemplate {
  context?: Record<string, string>;
  format: 2;
  markdown: string;
  sections: {
    key: string;
    title: string;
    shape: string;
    options: ScriptOption[];
    pick: [number, number] | null;
  }[];
  blocks: {
    id: string;
    instruction: string;
    section: string;
    option: string;
  }[];
  facts: string[];
  never_say: string[];
}
export interface ScriptError {
  line: number;
  message: string;
}
export interface Script {
  context?: Record<string, string>;
  sections?: ScriptSection[];
  picks?: Record<string, string[]>;
  written?: Record<string, { text: string; fact_ids: string[] }>;
  offers?: { title: string | null; text: string }[];
  summary?: string;
  preparation_status?: string;
  opener: string;
  gatekeeper?: string;
  closing_question?: string;
  template_version?: string;
  screening_intro: string;
  discovery_questions: string[];
  script_version: string;
  objection_cards: { id: string; text: string; tags: string[] }[];
  fallback: boolean;
  ready_for_rep: boolean;
}
export interface Fact {
  id: string;
  fact_id: string;
  statement: string;
  source_id: string;
  source_url: string;
  status: string;
  expires_at: string;
}
export interface Packet {
  context_coverage?: {
    calls_total: number;
    calls_omitted: number;
    tasks_total: number;
    tasks_omitted: number;
  };
  review_token?: string;
  preparation?: { status: string; as_of?: string; reason?: string };
  tasks?: FollowupTask[];
  prospect_brief?: {
    freshness?: { state: string; preparation_revision: number };
    version: number;
    evidence_status: string;
    summary: string;
    next_step?: string | null;
    earlier_calls?: {
      call_id: string;
      at: string | null;
      outcome: string | null;
      summary: string | null;
      next_step: string | null;
      note: string | null;
    }[];
    human_notes: { call_id: string; text: string }[];
    next_question: string;
    last_outcome: string | null;
    meeting: {
      correction_reason?: string;
      status: string;
      due_at: string | null;
      timezone: string | null;
      source: string;
      held: boolean;
      invitation_sent: false;
    } | null;
  };
  statistical_guidance?: {
    human_probability: number;
    right_party_given_human: number;
    conversion_given_right_party: number;
    conversion_goal: string;
    samples: number;
    conversion_samples: number;
    prior_only?: boolean;
    estimate_level?: string;
    estimate_policy?: EstimatePolicy | null;
    estimate_basis?: "pooled" | "reviewed_model";
    source?:
      | "frozen_call_decision"
      | "frozen_reservation_decision"
      | "current_pooled_estimate";
    decision_id?: string | null;
    estimate_policy_version?: string;
    work_assumption_seconds?: number | null;
    stage_support?: Record<
      string,
      {
        cell_known: number;
        cell_successes: number;
        cell_unknown: number;
        conditioning_excluded: number;
        outside_known: number;
        observed_interval: [number, number];
        interval_kind: string;
      }
    >;
    uncertainty: string;
    why_now: string;
    as_of: string | null;
  };
  lead: Lead;
  script: Script;
  research: {
    facts: Fact[];
    sources: { id: string; title: string; url: string; retrieved_at: string }[];
  };
  previous_calls: Call[];
  bound_number_id: string | null;
}
export interface Reservation {
  reservation_id: string;
  expires_at: string;
  lead: Lead;
  number: NumberRecord;
}
export interface CapabilityStatus {
  id: string;
  state:
    | "unconfigured"
    | "configured"
    | "software_verified"
    | "live_verified"
    | "degraded"
    | "unavailable";
  blocks: string;
  reason_codes: string[];
  evidence_at: string | null;
  remedy: string;
  source_version: string;
}
export interface Readiness {
  capabilities?: CapabilityStatus[];
  capabilities_as_of?: string;
  capabilities_stale?: boolean;
  integrity_ok: boolean | null;
  checks_scope?: string;
  ready_to_call: boolean | null;
  blockers: string[];
  warnings: string[];
  mode: string;
  simulation: boolean;
  version: string;
  screening?: {
    mode: string;
    configured: boolean;
    authorized: boolean;
    fresh: boolean;
    cleared_count: number;
    pending_count: number;
    blocked_count: number;
    dialable_queue_count: number;
    inspected_count: number;
    total_count: number;
    omitted_count: number;
    counts_complete: boolean;
    reason_codes: string[];
  };
}
export interface Policy {
  id: string;
  /** Row version of this immutable publication; publishing an older one is a conflict. */
  version?: number;
  approved?: boolean;
  /** Optional national do-not-call screening before calls; off by default. */
  dnc_screening?: boolean;
  state_registry_required: boolean;
  local_start_hour: number;
  local_end_hour: number;
  max_attempts: number;
  permissions: string[];
  review_reference?: string;
  reason?: string;
  voicemail_enabled?: boolean;
  voicemail_optout_supported?: boolean;
  approved_assets?: string[];
}
export interface Playbook {
  id: string;
  version: number;
  name: string;
  approved: boolean;
  opener: string;
  screening_intro: string;
  product_description?: string;
  claims?: string[];
  prohibited_claims?: string[];
  cards: { id: string; text: string; tags: string[] }[];
}
export interface HistoryPage<T> {
  items: T[];
  total: number | null;
  /** The server stopped counting at its cap: `total` is a lower bound. */
  total_capped?: boolean;
  next_cursor: string | null;
  search_complete?: boolean;
  scanned?: number;
  query?: string;
  filter?: string;
}
export interface Workspace {
  /** Latest live-event sequence reflected by this snapshot. */
  event_cursor?: number;
  quiescence?: { can_disconnect: boolean; reasons: string[] };
  reservation?: Reservation | null;
  active_playbook?: Playbook | null;
  pages?: Record<string, Omit<HistoryPage<never>, "items">>;
  mode: string;
  simulation: boolean;
  seller: { id: string; name: string; approved: boolean };
  user: {
    id: string;
    name: string;
    presence: {
      status: string;
      audio_ready?: boolean;
      agent_call_control_id?: string;
    };
  };
  campaign: { id: string; name: string; policy_id: string };
  policy: Policy;
  leads: Lead[];
  numbers: NumberRecord[];
  calls: Call[];
  readiness: Readiness;
  playbooks: Playbook[];
  registries: {
    id: string;
    version: number;
    kind: string;
    jurisdiction?: string;
    published: boolean;
    subscription_reference?: string;
    subscription_id?: string;
    authorization_expires_at?: string;
    npas: string[];
    refreshed_at: string;
    checksum: string;
    synthetic?: boolean;
  }[];
  jobs: {
    id: string;
    queue: string;
    status: string;
    attempts: number;
    error?: string;
  }[];
  trackers: { id: string; name: string; kind: string; version: number }[];
  models: {
    id: string;
    active: boolean;
    trial_active?: boolean;
    trial_ends_at?: string;
    goal?: string;
    feature_schema_version?: number;
    reviewable?: boolean;
    objective_stage?: string;
    window_days?: number;
    pooled_stages?: string[];
    bootstrap?: { groups: number; interval: [number, number] | null };
    conversation_ablation?: { eligible_for_serving: boolean };
    rolling_evaluations?: {
      training_cutoff: string;
      calibration_cutoff: string;
      evaluation_cutoff: string;
      stages: Record<
        string,
        {
          status: string;
          support: Record<string, { positive: number; negative: number }>;
        }
      >;
    }[];
    calibration?: {
      expected_calibration_error: number | null;
      bins: {
        lower: number;
        upper: number;
        samples: number;
        mean_prediction: number;
        observed_rate: number;
      }[];
      subgroups: Record<
        string,
        Record<
          string,
          { samples: number; brier: number; seen_during_training: boolean }
        >
      >;
    };
    metrics: Record<string, number | null>;
    checksum: string;
  }[];
  tasks: FollowupTask[];
  task_owners?: { id: string; name: string }[];
}
export interface EstimatePolicy {
  human_prior: [number, number];
  right_prior: [number, number];
  goal_prior: [number, number];
  shrinkage: number;
  work_prior: number;
}
export interface EvidenceItem {
  text: string;
  evidence: string[];
}
/** One call in Review (R10.9): outcome, after-call summary, the rep's note and the 30-day transcript. */
export interface CallReview {
  call: Call;
  lead_timezone?: string | null;
  summary: Call["aftercall"];
  human_notes: string | null;
  transcript: Segment[];
  metrics: Record<string, unknown>;
  script_trace?: {
    sections: {
      key: string;
      reaction: string;
      portion_ms: number | null;
      prospect_words: number;
    }[];
    opening: {
      key: string;
      reaction: string;
      first_response_ms: number | null;
    } | null;
    shape: {
      timing: string;
      rep_talk_ratio: number | null;
      longest_monologue_ms: number | null;
      time_to_first_question_ms: number | null;
      discovery_questions_asked: number;
      duration_ms: number | null;
    };
  } | null;
  raw_evidence_status?: string;
}
/** No call, or one whose carrier leg has finished. */
export const isTerminal = (call: { state: string } | null | undefined) =>
  !call || TERMINAL_STATES.has(call.state);
/** The rep is still on the line: the call has not finished. */
export const isActiveCall = (call: { state: string } | null | undefined) =>
  !!call && !isTerminal(call);
