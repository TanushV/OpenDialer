import { useCallback, useEffect, useRef, useState } from "react";
import { date, isAdmin, label, percent, useWorkspace } from "../context";
import { limits, useDefinitions } from "../state/definitions";
import { Experiments, type ControlledComparison } from "./TrialControls";
import { DatedStaffing, type StaffedWindow } from "./Staffing";
import type { EstimatePolicy } from "../types";

export interface HealthPolicy {
  min_comparable: number;
  material_drop: number;
  confidence: number;
  window_days: number;
  callback_proof_days: number;
}
export interface OptimizationSettings {
  conversion_goal: string;
  window_days: number;
  exploration: number;
  auto_train: boolean;
  horizon_hours: number;
  rep_minutes: number;
  handling_seconds: number;
  daily_capacity_per_number: number;
  training_min_rows: number;
  staffed_windows: StaffedWindow[];
  additional_ready_after_hours: number;
  staffing_timezone: string;
  staffing_start_hour: number;
  staffing_end_hour: number;
  staffing_reps: number;
  staffing_weekdays: number[];
  account_channel_limit: number | null;
  minimum_expansion_age_hours: number;
  estimate_policy?: EstimatePolicy;
  health_policy?: HealthPolicy;
}
export interface OptimizationReport {
  settings: OptimizationSettings;
  metrics: {
    original_eligible_leads: number;
    unique_conversions: number;
    unique_right_party: number;
    conversion_per_original_eligible_lead: number | null;
    conversions_per_rep_hour: number | null;
  };
  dataset: {
    rows: number;
    matured_rows: number;
    pending_rows: number;
    positive_conversions: number;
    missing_snapshot_calls: number;
  };
  decisions: {
    id: string;
    lead_id: string;
    propensity: number;
    experiment_arm: string;
    allocation?: { reason: string; reassignment_reason: string | null };
  }[];
  future_slots: {
    lead_id: string;
    at: string;
    reason: string;
    name: string;
  }[];
  drift: {
    action: string;
    model_versions?: string[];
    missingness_shift?: boolean;
    windows?: string;
    stage_support?: Record<
      string,
      {
        reference: number;
        recent: number;
        paired_support: number;
        reference_brier: number | null;
        recent_brier: number | null;
      }
    >;
  };
  trial: {
    epoch: string;
    sufficient_evidence: boolean;
    pending_leads: number;
    required_per_arm: number;
    arms: Record<
      string,
      {
        leads: number;
        matured_leads: number;
        conversions: number;
        rate: number | null;
        confidence_low: number;
        confidence_high: number;
      }
    >;
  };
  comparisons: ControlledComparison[];
  cohort_history: string[];
  coaching: {
    reviewed_cards: number;
    useful_fraction: number | null;
    grounded_fraction: number | null;
    timely_fraction: number | null;
  };
}
interface ReportProjection {
  status: "pending" | "stale" | "ready";
  as_of: string | null;
  calculated_settings?: OptimizationSettings;
  refresh_status?: string | null;
  refresh_error?: string | null;
}
type ReportResponse =
  | (OptimizationReport & { projection: ReportProjection })
  | { settings: OptimizationSettings; projection: ReportProjection };

/** One saved-report read; pending, stale and failed reads are explicit and retryable. */
export function useOptimizationReport() {
  const { api, subscribe } = useWorkspace();
  const [response, setResponse] = useState<ReportResponse | null>(null);
  const [error, setError] = useState("");
  const epoch = useRef<string | null>(null);
  const requestVersion = useRef(0);
  const load = useCallback(async () => {
    const version = ++requestVersion.current;
    try {
      const next = await api<ReportResponse>(
        "/v1/optimization" +
          (epoch.current ? "?epoch=" + encodeURIComponent(epoch.current) : ""),
      );
      if (version !== requestVersion.current) return;
      setResponse(next);
      setError("");
    } catch (reason) {
      if (version === requestVersion.current)
        setError(reason instanceof Error ? reason.message : String(reason));
      throw reason;
    }
  }, [api]);
  useEffect(() => {
    void load().catch(() => {});
    let pending: ReturnType<typeof setTimeout> | null = null;
    const stop = subscribe((events) => {
      if (
        !pending &&
        events.some((e) =>
          [
            "optimization.updated",
            "conversion.recorded",
            "number.updated",
          ].includes(e.type),
        )
      )
        pending = setTimeout(() => {
          pending = null;
          void load().catch(() => {});
        }, 150);
    });
    return () => {
      stop();
      requestVersion.current++;
      if (pending) clearTimeout(pending);
    };
  }, [load, subscribe]);
  const selectEpoch = useCallback(
    async (value: string | null) => {
      epoch.current = value;
      setResponse(null);
      await load();
    },
    [load],
  );
  const report = response && "metrics" in response ? response : null;
  return {
    response,
    report,
    error,
    load,
    epoch: epoch.current,
    selectEpoch,
  };
}
export type ReportState = ReturnType<typeof useOptimizationReport>;

function ReportStatus({ state }: { state: ReportState }) {
  const { run } = useWorkspace();
  const projection = state.response?.projection;
  if (!state.response)
    return (
      <section className="card padded" aria-label="Report freshness">
        <p role="status">
          {state.error
            ? `Learning evidence could not be loaded: ${state.error}`
            : "Loading allocation and learning evidence…"}
        </p>
        {state.error && (
          <button className="secondary" onClick={() => void run(state.load)}>
            Retry loading the report
          </button>
        )}
      </section>
    );
  return (
    <section className="card padded" aria-label="Report freshness">
      <p>
        {projection?.status === "pending"
          ? "Preparing the first report in the background."
          : projection?.refresh_status === "dead"
            ? "Saved report; refresh failed. Review the job in Recovery."
            : projection?.status === "stale"
              ? "Showing saved evidence while the report refreshes."
              : "Report is current."}
      </p>
      {projection?.as_of && (
        <p className="fine">Calculated {date(projection.as_of)}.</p>
      )}
      {projection?.calculated_settings && (
        <p className="fine">
          Calculated for{" "}
          {label(projection.calculated_settings.conversion_goal).toLowerCase()},{" "}
          {projection.calculated_settings.window_days} days
          {projection.status === "stale"
            ? "; totals may differ from current settings."
            : "."}
        </p>
      )}
      {projection?.refresh_error && projection.refresh_status !== "dead" && (
        <p className="fine">
          The last refresh failed; a bounded retry is pending.
        </p>
      )}
      {state.error && (
        <p role="status" className="notice">
          The latest read failed: {state.error}
        </p>
      )}
      {projection?.status !== "ready" && (
        <button className="secondary" onClick={() => void run(state.load)}>
          Check report status
        </button>
      )}
      {state.epoch && (
        <button
          className="secondary"
          onClick={() => void run(() => state.selectEpoch(null))}
        >
          Return to current cohort
        </button>
      )}
    </section>
  );
}

/** Intelligence: measured outcomes for the saved goal. Configuration lives in Settings. */
export function OptimizationPanel() {
  const { api, run, notice, refresh, snapshot: w } = useWorkspace();
  const state = useOptimizationReport();
  const { response, report } = state;
  return (
    <>
      <ReportStatus state={state} />
      {response && (
        <section className="card padded optimization-panel">
          <span className="eyebrow">PICKUP AND CONVERSION</span>
          <h2>Learn from the whole eligible cohort</h2>
          {report && (
            <p className="muted small">
              {report.metrics.unique_conversions} unique conversions /{" "}
              {report.metrics.original_eligible_leads} original eligible leads.{" "}
              {report.metrics.unique_right_party} people reached. Conversion
              rate:{" "}
              {percent(
                report.metrics.conversion_per_original_eligible_lead,
                "Awaiting evidence",
              )}
              .
            </p>
          )}
          {response.settings.conversion_goal && (
            <p className="fine" data-testid="saved-goal">
              Goal: {label(response.settings.conversion_goal)} within{" "}
              {response.settings.window_days} days.
              {isAdmin(w.user.role) &&
                " Change the goal and schedule in Settings → More settings."}
            </p>
          )}
          {isAdmin(w.user.role) && (
            <button
              className="primary"
              onClick={() =>
                void run(async () => {
                  await api("/v1/admin/optimization/refresh", "POST", {});
                  await state.load();
                  await refresh();
                  notice("Learning and capacity refresh queued");
                })
              }
            >
              Refresh learning and capacity
            </button>
          )}
        </section>
      )}
      {report && (
        <section className="card padded optimization-panel">
          <h2>Future calling opportunities</h2>
          <div className="optimization-table">
            <table>
              <thead>
                <tr>
                  <th>Lead</th>
                  <th>Suggested time</th>
                  <th>Reason</th>
                </tr>
              </thead>
              <tbody>
                {report.future_slots.slice(0, 12).map((slot) => (
                  <tr key={slot.lead_id}>
                    <td>{slot.name}</td>
                    <td>{date(slot.at)}</td>
                    <td>{label(slot.reason)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {!report.future_slots.length && (
            <p className="fine">No future opportunities in this report.</p>
          )}
        </section>
      )}
      {report && (
        <section className="card padded optimization-panel">
          <h2>Coaching quality</h2>
          <p>
            {report.coaching.reviewed_cards} reviewed cards · Useful{" "}
            {percent(report.coaching.useful_fraction, "Awaiting evidence")} ·
            Grounded{" "}
            {percent(report.coaching.grounded_fraction, "Awaiting evidence")} ·
            Timely{" "}
            {percent(report.coaching.timely_fraction, "Awaiting evidence")}.
          </p>
        </section>
      )}
    </>
  );
}

/** Settings → Advanced: planning assumptions, experiments and learning diagnostics. */
export function LearningAdvanced() {
  const state = useOptimizationReport();
  const { response, report } = state;
  return (
    <>
      <ReportStatus state={state} />
      {response && (
        <PlanningAssumptions
          settings={response.settings}
          onSaved={state.load}
        />
      )}
      {report && <LearningDiagnostics report={report} />}
      <Experiments state={state} />
    </>
  );
}

const ADVANCED_KEYS = [
  "exploration",
  "horizon_hours",
  "rep_minutes",
  "handling_seconds",
  "daily_capacity_per_number",
  "training_min_rows",
  "auto_train",
  "account_channel_limit",
  "minimum_expansion_age_hours",
  "additional_ready_after_hours",
  "staffed_windows",
  "health_policy",
] as const;
type AdvancedKey = (typeof ADVANCED_KEYS)[number];
type Assumptions = Pick<OptimizationSettings, AdvancedKey>;
const pick = (settings: OptimizationSettings) =>
  Object.fromEntries(
    ADVANCED_KEYS.map((key) => [key, settings[key]]),
  ) as Assumptions;

function PlanningAssumptions({
  settings,
  onSaved,
}: {
  settings: OptimizationSettings;
  onSaved: () => Promise<void>;
}) {
  const { api, run, notice, refresh } = useWorkspace();
  const bounds = useDefinitions();
  const b = bounds?.optimization;
  const h = bounds?.health_policy;
  const [draft, setDraft] = useState<Assumptions>(() => pick(settings));
  const dirty = useRef(false);
  const editRevision = useRef(0);
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    if (!dirty.current) setDraft(pick(settings));
  }, [settings]);
  const change = <K extends AdvancedKey>(key: K, value: Assumptions[K]) => {
    dirty.current = true;
    editRevision.current++;
    setDraft((old) => ({ ...old, [key]: value }));
  };
  const number = (key: AdvancedKey, title: string, step?: string) => (
    <label key={key}>
      {title}
      <input
        type="number"
        step={step}
        {...limits(b?.[key])}
        value={(draft[key] as number | null) ?? ""}
        onChange={(e) =>
          change(
            key,
            (key === "account_channel_limit" && !e.target.value
              ? null
              : Number(e.target.value)) as never,
          )
        }
      />
    </label>
  );
  const estimate = settings.estimate_policy;
  return (
    <section
      className="card padded optimization-panel"
      aria-label="Planning assumptions"
    >
      <h2>Planning assumptions</h2>
      <p className="fine">
        Engineering starting values for forecasts and learning. Calling
        permissions, callbacks, stable caller identity and coverage stay
        mandatory; nothing here authorizes a number purchase.
      </p>
      <div className="optimization-fields">
        {number("exploration", "Exploration share", "0.01")}
        {number("horizon_hours", "Forecast horizon (hours)")}
        {number("rep_minutes", "Available rep minutes")}
        {number("handling_seconds", "Average handling time (seconds)")}
        {number(
          "daily_capacity_per_number",
          "Reviewed daily capacity per number",
        )}
        {number("training_min_rows", "Minimum training rows")}
        {number(
          "account_channel_limit",
          "Account concurrent-call limit (blank if unknown)",
        )}
        {number(
          "minimum_expansion_age_hours",
          "Shortfall review interval (hours)",
        )}
        {number(
          "additional_ready_after_hours",
          "New number setup delay (hours)",
        )}
      </div>
      <label className="check-row">
        <input
          type="checkbox"
          checked={draft.auto_train}
          onChange={(e) => change("auto_train", e.target.checked)}
        />
        Automatic training from call history
      </label>
      <DatedStaffing
        windows={draft.staffed_windows || []}
        zone={settings.staffing_timezone ?? "UTC"}
        bounds={bounds}
        onChange={(value) => change("staffed_windows", value)}
      />
      {draft.health_policy && (
        <details>
          <summary>Advanced number evidence policy</summary>
          <p className="fine">
            Review assumptions. A statistical warning is not proof of a carrier
            spam label. Changes require fresh health evaluation; calling
            identity is preserved.
          </p>
          <div className="optimization-fields">
            {(
              [
                ["min_comparable", "Minimum comparable attempts"],
                ["material_drop", "Material pickup drop (fraction)"],
                ["confidence", "Health interval confidence (fraction)"],
                ["window_days", "Health observation window (days)"],
                ["callback_proof_days", "Callback proof lifetime (days)"],
              ] as const
            ).map(([key, title]) => (
              <label key={key}>
                {title}
                <input
                  type="number"
                  step="any"
                  {...limits(h?.[key])}
                  value={draft.health_policy![key]}
                  onChange={(e) =>
                    change("health_policy", {
                      ...draft.health_policy!,
                      [key]: Number(e.target.value),
                    })
                  }
                />
              </label>
            ))}
          </div>
        </details>
      )}
      {estimate && (
        <details>
          <summary>Sparse estimate assumptions</summary>
          <p className="fine" data-testid="estimate-assumptions">
            Human pickup prior {estimate.human_prior[0]} successes /{" "}
            {estimate.human_prior[1]} failures · Right party prior{" "}
            {estimate.right_prior[0]} / {estimate.right_prior[1]} · Selected
            goal prior {estimate.goal_prior[0]} / {estimate.goal_prior[1]} ·
            Pooled shrinkage weight {estimate.shrinkage} · Work prior{" "}
            {estimate.work_prior} attempt equivalents.
          </p>
          <p className="fine">
            Read-only starting weights. They apply until observed outcomes
            provide support; changing them requires renewed model evaluation.
          </p>
        </details>
      )}
      <button
        className="secondary"
        disabled={saving}
        onClick={() =>
          void run(async () => {
            const revision = editRevision.current;
            setSaving(true);
            try {
              const original = pick(settings);
              const changed = Object.fromEntries(
                ADVANCED_KEYS.filter(
                  (key) =>
                    JSON.stringify(draft[key]) !==
                    JSON.stringify(original[key]),
                ).map((key) => [key, draft[key]]),
              );
              await api("/v1/admin/optimization", "PUT", changed);
              if (editRevision.current === revision) dirty.current = false;
              await onSaved();
              await refresh();
              notice("Planning assumptions saved");
            } finally {
              setSaving(false);
            }
          })
        }
      >
        Save planning assumptions
      </button>
    </section>
  );
}

function LearningDiagnostics({ report }: { report: OptimizationReport }) {
  const { snapshot: w } = useWorkspace();
  return (
    <section
      className="card padded optimization-panel"
      aria-label="Learning diagnostics"
    >
      <h2>Operational training dataset</h2>
      <p>
        {report.dataset.rows} observed attempts · {report.dataset.matured_rows}{" "}
        closed windows · {report.dataset.pending_rows} pending outcomes ·{" "}
        {report.dataset.positive_conversions} observed conversions.
      </p>
      {report.dataset.missing_snapshot_calls > 0 && (
        <p className="fine">
          {report.dataset.missing_snapshot_calls} older attempts lack pre-call
          snapshots and are retained in history without inventing training
          features.
        </p>
      )}
      <p className="fine">Drift: {label(report.drift.action)}.</p>
      {report.drift.stage_support && (
        <details>
          <summary>Drift support and missing data</summary>
          <p>
            {report.drift.windows}. Compared models:{" "}
            {report.drift.model_versions?.join(", ") ||
              "No comparable model evidence"}
            .
          </p>
          {report.drift.missingness_shift && (
            <p role="status">
              Missing input coverage changed; check sources before interpreting
              performance.
            </p>
          )}
          {Object.entries(report.drift.stage_support).map(([stage, v]) => (
            <p key={stage}>
              {label(stage)}: {v.reference} reference / {v.recent} recent
              classified outcomes; {v.paired_support} comparable support.
            </p>
          ))}
        </details>
      )}
      <details>
        <summary>Recent allocation and treatment decisions</summary>
        {report.decisions
          .slice(-8)
          .reverse()
          .map((decision) => (
            <p className="fine" key={decision.id}>
              {w.leads.find((l) => l.id === decision.lead_id)?.name ||
                "Retained lead"}
              : {label(decision.allocation?.reason || "authorized allocation")}.{" "}
              {decision.allocation?.reassignment_reason} Treatment probability{" "}
              {percent(decision.propensity)}; {label(decision.experiment_arm)}.
            </p>
          ))}
      </details>
    </section>
  );
}
