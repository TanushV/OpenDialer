export interface Session {
  access_token: string;
  tenant_id: string;
  seller_id: string;
  user_id: string;
  role: string;
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
  status: string;
  suppressed: boolean;
  coverage_due_at: string;
  attempt_count: number;
  callback_due_at?: string;
  outreach_hold?: string | null;
}
export interface FollowupTask {
  id: string;
  version: number;
  inbound?: boolean;
  inbound_state?: string;
  routed_rep_id?: string | null;
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
  review_required?: boolean;
  time_window?: {
    date: string;
    period: string | null;
    timezone: string | null;
    local_time?: string;
  } | null;
}
export interface NumberRecord {
  id: string;
  phone: string;
  region: string;
  lifecycle: string;
  daily_limit: number;
  capacity: number;
  identity_configured: boolean;
  callback_tested: boolean;
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
export interface Coach {
  pinned?: boolean;
  private?: boolean;
  _expires?: number;
  _monoExpires?: number;
  id: string;
  action: string;
  kind: string;
  say_or_ask: string;
  reason: string;
  expires_at?: string;
  remaining_ms?: number;
  provider?: string;
  latency?: Record<string, number | null>;
  state_version: number;
  evidence_segment_ids: string[];
}
export interface Call {
  rubric_snapshot?: { call_type: string; rubric_version: string };
  rubric_revision?: number;
  version: number;
  raw_evidence_status?: string;
  analysis_incomplete?: boolean;
  unprocessed_speech_events?: number;
  coach_preferences?: { muted?: boolean; pinned?: boolean };
  detached?: boolean;
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
  conversation_state?: {
    stage: string;
    trigger: string;
    version: number;
  } | null;
}
export interface Script {
  opener: string;
  gatekeeper?: string;
  proof?: string;
  closing_question?: string;
  template_version?: string;
  screening_intro: string;
  discovery_questions: string[];
  script_version: string;
  playbook_version: string;
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
  review_token?: string;
  preparation?: { status: string; as_of?: string };
  tasks?: FollowupTask[];
  prospect_brief?: {
    version: number;
    evidence_status: string;
    summary: string;
    needs: EvidenceItem[];
    unresolved: EvidenceItem[];
    unknowns: string[];
    observations?: {
      id: string;
      concept: string;
      approach: string | null;
      review_status: string;
    }[];
    human_notes: { call_id: string; text: string }[];
    next_question: string;
    last_outcome: string | null;
    meeting: {
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
  commitments: { action: string; due_at: string | null }[];
  bound_number_id: string | null;
}
export interface Reservation {
  reservation_id: string;
  expires_at: string;
  lead: Lead;
  number: NumberRecord;
}
export interface Readiness {
  integrity_ok: boolean;
  ready_to_call: boolean;
  blockers: string[];
  warnings: string[];
  mode: string;
  simulation: boolean;
  version: string;
}
export interface Policy {
  id: string;
  approved: boolean;
  registry_required: boolean;
  state_registry_required: boolean;
  local_start_hour: number;
  local_end_hour: number;
  max_attempts: number;
  permissions: string[];
  review_reference: string;
  reason?: string;
  voicemail_enabled?: boolean;
  voicemail_optout_supported?: boolean;
  approved_assets?: string[];
  detached_topology_verified?: boolean;
  topology_test_reference?: string;
  late_human_recovery_destination?: string;
}
export interface Playbook {
  id: string;
  name: string;
  approved: boolean;
  opener: string;
  screening_intro: string;
  cards: { id: string; text: string; tags: string[] }[];
}
export interface Workspace {
  mode: string;
  simulation: boolean;
  seller: { id: string; name: string; approved: boolean };
  user: {
    id: string;
    name: string;
    role: string;
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
  metrics: {
    original_cohort: number;
    eligible_now: number;
    right_party_unique: number;
    held_meetings: number;
    coverage_overdue: number;
    suppressed: number;
    reserved_cost_usd: number;
    billing_verified: boolean;
    rep_work_seconds?: number | null;
    conversions_per_rep_hour?: number | null;
    work_time?: { note: string };
    outcome_goal?: string;
    outcome_window_days?: number;
    warnings?: string[];
    health?: {
      connect_rate: {
        status: string;
        samples: number;
        unknown: number;
        rate: number | null;
        reference_rate: number | null;
        comparison: string;
        confidence_low: number;
        confidence_high: number;
        all_attempt_rate: number | null;
      };
    };
  };
  readiness: Readiness;
  playbooks: Playbook[];
  registries: {
    id: string;
    kind: string;
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
    metrics: Record<string, number>;
    checksum: string;
  }[];
  tasks: FollowupTask[];
  task_owners?: { id: string; name: string }[];
}
export interface EvidenceItem {
  text: string;
  evidence: string[];
}
export interface Analysis {
  trackers?: {
    tracker_id: string;
    name?: string;
    kind?: string;
    version?: number;
    evidence: string[];
    raw_evidence_status?: string;
  }[];
  tracker_definitions_omitted?: number;
  observations?: {
    id: string;
    version: number;
    concept: string;
    approach: string | null;
    status: string;
    review_status: string;
    observed_at: string;
    evidence: string[];
  }[];
  raw_evidence_status?: string;
  grounding_status?: "unreviewed" | "model_reviewed" | "review_required";
  call: Call;
  notes: {
    summary: string;
    outcome: string;
    transcript_version: number;
    problems: EvidenceItem[];
    objections: EvidenceItem[];
    commitments: {
      action: string;
      due_at: string | null;
      evidence: string[];
      needs_time_confirmation: boolean;
    }[];
    unknowns: string[];
  };
  notes_version: number;
  human_notes: string | null;
  suggested_update: unknown;
  status: string;
  scorecard_id: string;
  scorecard_version: number;
  reviewed_scorecard?: Analysis["scorecard"];
  scorecard: {
    call_type?: string;
    rubric_version?: string;
    title?: string;
    purpose?: string;
    overall: { score: number | null; coverage: number };
    items: {
      dimension: string;
      title?: string;
      source?: string;
      score: number | null;
      status: string;
      explanation: string;
      evidence: string[];
    }[];
  };
  transcript: Segment[];
  metrics: Record<string, unknown>;
  reviews: {
    id?: string;
    reason: string;
    status: string;
    reviewer_id: string;
    reviewer_role?: string;
    at?: string;
    resolution_id?: string | null;
    applies?: boolean;
    raw_evidence_status?: string;
  }[];
  comments: { text: string; author_id: string }[];
  recording_available: boolean;
  provider: string;
}
export const terminal = (call: Call | null) =>
  !call ||
  [
    "ENDED",
    "FINALIZED",
    "BUSY",
    "NO_ANSWER",
    "INVALID_ENDPOINT",
    "TEMPORARY_FAILURE",
  ].includes(call.state);
