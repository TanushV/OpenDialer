import { useCallback, useEffect, useRef, useState } from "react";
import { date, label, useWorkspace } from "../context";
import { TrialControls, type ControlledComparison } from "./TrialControls";
import { ConversationLearning } from "./ConversationLearning";
import { Staffing, type StaffedWindow, type SimpleStaffing } from "./Staffing";
import type { EstimatePolicy } from "../types";

interface OptimizationSettings extends SimpleStaffing {
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
  estimate_policy?: EstimatePolicy;
  health_policy?: {
    min_comparable: number;
    material_drop: number;
    confidence: number;
    window_days: number;
    callback_proof_days: number;
  };
}
interface GoalPreview {
  campaign_version: number;
  from_goal: string;
  to_goal: string;
  completed_before: number;
  completed_after: number;
  requires_next_action: number;
  preserved_holds: number;
  preserved_callbacks: number;
}
interface OptimizationReport {
  settings: OptimizationSettings;
  forecast: {
    capacity_shortfall: number;
    staffing_shortfall: number | null;
    number_shortfall: number | null;
    planning_limited?: boolean;
    regions: {
      region: string;
      forecast_attempts: number;
      planned_attempts: number;
      usable_existing_capacity: number;
      general_fallback_capacity: number;
      capacity_waiting_for_review: number;
      proposed_additional: number | null;
    }[];
  };
  numbers: {
    id: string;
    phone: string;
    daily_limit: number;
    lifecycle: string;
    active_obligations: number;
    health: {
      score: number | null;
      samples: number;
      status: string;
      reason: string;
    };
  }[];
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
    snapshots: { id: string; rows: number; goal: string; as_of: string }[];
  };
  decisions: {
    id: string;
    lead_id: string;
    action: string;
    propensity: number;
    experiment_arm: string;
    allocation?: { reason: string; reassignment_reason: string | null };
  }[];
  future_slots: {
    lead_id: string;
    at: string;
    local_hour: number;
    reason: string;
    name: string;
  }[];
  drift: {
    action: string;
    drift_detected: boolean;
    recent_brier: number | null;
    reference_brier: number | null;
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
    latency_support?: Record<string, number>;
    latency_quantization?: { upper_bounds: boolean };
    useful_fraction: number | null;
    grounded_fraction: number | null;
    timely_fraction: number | null;
    distracting_fraction: number | null;
    p95_inference_ms: number | null;
    p95_delivery_ms: number | null;
    p95_state_ms: number | null;
    p95_retrieval_ms: number | null;
    p95_event_to_render_ms: number | null;
    p95_render_ms: number | null;
  };
}
interface ReportProjection {
  status: "pending" | "stale" | "ready";
  as_of: string | null;
  calculated_settings?: OptimizationSettings;
  refresh_status?: string | null;
  refresh_error?: string | null;
  job_id?: string | null;
}
type ReportResponse =
  | (OptimizationReport & { projection: ReportProjection })
  | { settings: OptimizationSettings; projection: ReportProjection };

const percent = (value: number | null | undefined) =>
  value == null ? "Awaiting evidence" : (100 * value).toFixed(1) + "%";

export function OptimizationPanel({
  numbersOnly = false,
}: {
  numbersOnly?: boolean;
}) {
  const { api, run, notice, refresh, snapshot: w, subscribe } = useWorkspace();
  const [response, setResponse] = useState<ReportResponse | null>(null);
  const report = response && "metrics" in response ? response : null;
  const epoch = useRef<string | null>(null);
  const requestVersion = useRef(0);
  const [settings, setSettings] = useState<OptimizationSettings | null>(null);
  const dirty = useRef(false);
  const [goalPreview, setGoalPreview] = useState<GoalPreview | null>(null);
  const [replay, setReplay] = useState("");
  const [replayReference, setReplayReference] = useState("");
  const [replayResult, setReplayResult] = useState<string | null>(null);
  const [cohortReportId, setCohortReportId] = useState("");
  const admin = ["owner", "admin"].includes(w.user.role);
  const load = useCallback(async () => {
    const version = ++requestVersion.current;
    const next = await api<ReportResponse>(
      "/v1/optimization" +
        (epoch.current ? "?epoch=" + encodeURIComponent(epoch.current) : ""),
    );
    if (version !== requestVersion.current) return;
    setResponse(next);
    if (!dirty.current) setSettings(next.settings);
  }, [api]);
  useEffect(() => {
    void run(load);
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
          void run(load);
        }, 150);
    });
    return () => {
      stop();
      requestVersion.current++;
      if (pending) clearTimeout(pending);
    };
  }, [load, run, subscribe]);
  if (!response || !settings)
    return <p className="fine">Loading allocation and learning evidence…</p>;
  const change = <K extends keyof OptimizationSettings>(
    key: K,
    value: OptimizationSettings[K],
  ) => {
    dirty.current = true;
    setGoalPreview(null);
    setSettings({ ...settings, [key]: value });
  };
  return (
    <>
      {!numbersOnly && <ConversationLearning />}
      <section className="card padded" aria-label="Report freshness">
        <p>
          {response.projection.status === "pending"
            ? "Preparing the first report in the background."
            : response.projection.refresh_status === "dead"
              ? "Saved report; refresh failed. Review the job in Recovery."
              : response.projection.status === "stale"
                ? "Showing saved evidence while the report refreshes."
                : "Report is current."}
        </p>
        {response.projection.as_of && (
          <p className="fine">Calculated {date(response.projection.as_of)}.</p>
        )}
        {response.projection.calculated_settings && (
          <p className="fine">
            Calculated for{" "}
            {label(
              response.projection.calculated_settings.conversion_goal,
            ).toLowerCase()}
            , {response.projection.calculated_settings.window_days} days
            {response.projection.status === "stale"
              ? "; totals may differ from current settings."
              : "."}
          </p>
        )}
        {response.projection.refresh_error &&
          response.projection.refresh_status !== "dead" && (
            <p className="fine">
              The last refresh failed; a bounded retry is pending.
            </p>
          )}
        {response.projection.status !== "ready" && (
          <button className="secondary" onClick={() => void run(load)}>
            Check report status
          </button>
        )}
        {epoch.current && (
          <button
            className="secondary"
            onClick={() =>
              void run(async () => {
                epoch.current = null;
                setResponse(null);
                await load();
              })
            }
          >
            Return to current cohort
          </button>
        )}
      </section>
      {!numbersOnly && (
        <section className="card padded optimization-panel">
          <span className="eyebrow">PICKUP AND CONVERSION</span>
          <h2>Learn from the whole eligible cohort</h2>
          {report && (
            <p className="muted small">
              {report.metrics.unique_conversions} unique conversions /{" "}
              {report.metrics.original_eligible_leads} original eligible leads.{" "}
              {report.metrics.unique_right_party} people reached. Conversion
              rate:{" "}
              {percent(report.metrics.conversion_per_original_eligible_lead)}.
            </p>
          )}
          {admin && (
            <>
              <div className="optimization-fields">
                <label>
                  Conversion goal
                  <select
                    value={settings.conversion_goal}
                    onChange={(e) => change("conversion_goal", e.target.value)}
                  >
                    <option value="qualified_conversation">
                      Qualified conversation
                    </option>
                    <option value="meeting_held">Held meeting</option>
                    <option value="sale_won">Won sale</option>
                  </select>
                </label>
                <label>
                  Outcome window (days)
                  <input
                    type="number"
                    min={1}
                    max={180}
                    value={settings.window_days}
                    onChange={(e) =>
                      change("window_days", Number(e.target.value))
                    }
                  />
                </label>
                <label>
                  Exploration share
                  <input
                    type="number"
                    min={0}
                    max={0.3}
                    step={0.01}
                    value={settings.exploration}
                    onChange={(e) =>
                      change("exploration", Number(e.target.value))
                    }
                  />
                </label>
                <label>
                  Forecast horizon (hours)
                  <input
                    type="number"
                    min={1}
                    max={168}
                    value={settings.horizon_hours}
                    onChange={(e) =>
                      change("horizon_hours", Number(e.target.value))
                    }
                  />
                </label>
                <label>
                  Available rep minutes
                  <input
                    type="number"
                    min={0}
                    value={settings.rep_minutes}
                    onChange={(e) =>
                      change("rep_minutes", Number(e.target.value))
                    }
                  />
                </label>
                <label>
                  Average handling time (seconds)
                  <input
                    type="number"
                    min={10}
                    value={settings.handling_seconds}
                    onChange={(e) =>
                      change("handling_seconds", Number(e.target.value))
                    }
                  />
                </label>
                <label>
                  Reviewed daily capacity per number
                  <input
                    type="number"
                    min={1}
                    value={settings.daily_capacity_per_number}
                    onChange={(e) =>
                      change(
                        "daily_capacity_per_number",
                        Number(e.target.value),
                      )
                    }
                  />
                </label>
                <label>
                  Minimum training rows
                  <input
                    type="number"
                    min={100}
                    value={settings.training_min_rows}
                    onChange={(e) =>
                      change("training_min_rows", Number(e.target.value))
                    }
                  />
                </label>
              </div>
              {settings.estimate_policy && (
                <details>
                  <summary>Sparse estimate assumptions</summary>
                  <p className="fine">
                    Starting weights apply until observed outcomes provide
                    support. Changing these assumptions requires renewed model
                    evaluation.
                  </p>
                  <div className="optimization-fields">
                    {(
                      [
                        ["human_prior", "Human pickup"],
                        ["right_prior", "Right party"],
                        ["goal_prior", "Selected goal"],
                      ] as const
                    ).flatMap(([key, title]) =>
                      ([0, 1] as const).map((index) => (
                        <label key={`${key}:${index}`}>
                          {title} prior {index === 0 ? "successes" : "failures"}
                          <input
                            type="number"
                            min={0}
                            step="any"
                            value={settings.estimate_policy![key][index]}
                            onChange={(e) => {
                              const weights: [number, number] = [
                                ...settings.estimate_policy![key],
                              ];
                              weights[index] = Number(e.target.value);
                              change("estimate_policy", {
                                ...settings.estimate_policy!,
                                [key]: weights,
                              });
                            }}
                          />
                        </label>
                      )),
                    )}
                    {(
                      [
                        ["shrinkage", "Pooled shrinkage weight"],
                        ["work_prior", "Work prior attempt equivalents"],
                      ] as const
                    ).map(([key, title]) => (
                      <label key={key}>
                        {title}
                        <input
                          type="number"
                          min={0}
                          step="any"
                          value={settings.estimate_policy![key]}
                          onChange={(e) =>
                            change("estimate_policy", {
                              ...settings.estimate_policy!,
                              [key]: Number(e.target.value),
                            })
                          }
                        />
                      </label>
                    ))}
                  </div>
                </details>
              )}
              <Staffing
                simple={settings}
                onSimple={(value) => {
                  dirty.current = true;
                  setSettings({ ...settings, ...value });
                }}
                windows={settings.staffed_windows || []}
                delay={settings.additional_ready_after_hours || 0}
                onChange={(value) => change("staffed_windows", value)}
                onDelay={(value) =>
                  change("additional_ready_after_hours", value)
                }
              />
              <label className="check-row">
                <input
                  type="checkbox"
                  checked={settings.auto_train}
                  onChange={(e) => change("auto_train", e.target.checked)}
                />
                Automatic training from call history
              </label>
              {settings.health_policy && (
                <details>
                  <summary>Advanced number evidence policy</summary>
                  <p className="fine">
                    These are review assumptions. A statistical warning is not
                    proof of a carrier spam label. Changes require fresh health
                    evaluation; calling identity is preserved.
                  </p>
                  <div className="optimization-fields">
                    {(
                      [
                        ["min_comparable", "Minimum comparable attempts"],
                        ["material_drop", "Material pickup drop (fraction)"],
                        ["confidence", "Health interval confidence (fraction)"],
                        ["window_days", "Health observation window (days)"],
                        [
                          "callback_proof_days",
                          "Callback proof lifetime (days)",
                        ],
                      ] as const
                    ).map(([key, title]) => (
                      <label key={key}>
                        {title}
                        <input
                          type="number"
                          step="any"
                          value={settings.health_policy![key]}
                          onChange={(e) =>
                            change("health_policy", {
                              ...settings.health_policy!,
                              [key]: Number(e.target.value),
                            })
                          }
                        />
                      </label>
                    ))}
                  </div>
                </details>
              )}
              <div className="button-row">
                <button
                  className="secondary"
                  onClick={() =>
                    void run(async () => {
                      if (
                        settings.conversion_goal !==
                          response.settings.conversion_goal ||
                        settings.window_days !== response.settings.window_days
                      ) {
                        setGoalPreview(
                          await api<GoalPreview>(
                            "/v1/admin/optimization/preview",
                            "POST",
                            settings,
                          ),
                        );
                        return;
                      }
                      await api("/v1/admin/optimization", "PUT", settings);
                      dirty.current = false;
                      await load();
                      notice("Optimization settings saved");
                    })
                  }
                >
                  Save optimization settings
                </button>
                <button
                  className="primary"
                  onClick={() =>
                    void run(async () => {
                      await api("/v1/admin/optimization/refresh", "POST", {});
                      await load();
                      await refresh();
                      notice("Learning and capacity refresh queued");
                    })
                  }
                >
                  Refresh learning and capacity
                </button>
              </div>
              {goalPreview && (
                <section aria-label="Review goal change" className="alert">
                  <div>
                    <strong>
                      {label(goalPreview.from_goal)} →{" "}
                      {label(goalPreview.to_goal)}
                    </strong>
                    <p>
                      {goalPreview.completed_before} completed before;{" "}
                      {goalPreview.completed_after} completed under the new
                      goal.
                    </p>
                    <p>
                      Previously completed prospects require next-action review
                      before new outreach: {goalPreview.requires_next_action}.
                      Existing holds: {goalPreview.preserved_holds}; callbacks:{" "}
                      {goalPreview.preserved_callbacks}. Historical trials
                      retain their original definition.
                    </p>
                    <div className="button-row">
                      <button
                        className="primary"
                        onClick={() =>
                          void run(async () => {
                            try {
                              await api("/v1/admin/optimization", "PUT", {
                                ...settings,
                                expected_campaign_version:
                                  goalPreview.campaign_version,
                              });
                              dirty.current = false;
                              await load();
                              await refresh();
                              notice("Optimization settings saved");
                            } finally {
                              setGoalPreview(null);
                            }
                          })
                        }
                      >
                        Apply reviewed goal change
                      </button>
                      <button
                        className="secondary"
                        onClick={() => setGoalPreview(null)}
                      >
                        Cancel change
                      </button>
                    </div>
                  </div>
                </section>
              )}
              <p className="fine">
                Callbacks, calling permissions, stable identity and coverage
                remain mandatory. These settings do not authorize number
                purchases.
              </p>
            </>
          )}
        </section>
      )}
      {report && (
        <section className="card padded optimization-panel">
          <h2>Capacity before procurement</h2>
          <p className="muted small">
            Forecast includes due attempts, usable inventory and available rep
            time. New rentals are blocked; proposed additions are
            shared-capacity scenarios, not one number per area code.
          </p>
          <div className="optimization-table">
            <table>
              <thead>
                <tr>
                  <th>Region</th>
                  <th>Due attempts</th>
                  <th>Rep capacity allocated</th>
                  <th>Existing capacity</th>
                  <th>General fallback</th>
                  <th>Owned capacity awaiting review</th>
                  <th>Additional numbers</th>
                </tr>
              </thead>
              <tbody>
                {report.forecast.regions.map((region) => (
                  <tr key={region.region}>
                    <td>{region.region}</td>
                    <td>{region.forecast_attempts}</td>
                    <td>{region.planned_attempts}</td>
                    <td>{region.usable_existing_capacity}</td>
                    <td>{region.general_fallback_capacity}</td>
                    <td>{region.capacity_waiting_for_review}</td>
                    <td>{region.proposed_additional ?? "Not established"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {report.forecast.planning_limited ? (
            <p className="notice">
              Allocation is incomplete; remaining bottlenecks are not
              established.
            </p>
          ) : (
            report.forecast.capacity_shortfall > 0 && (
              <p className="fine">
                {report.forecast.staffing_shortfall} attempts exceed declared
                rep time; {report.forecast.number_shortfall} staffed attempts
                lack usable number capacity. These are separate constraints.
              </p>
            )
          )}
          <h3>Observed inventory snapshot</h3>
          <div className="optimization-table">
            <table>
              <thead>
                <tr>
                  <th>Number</th>
                  <th>State</th>
                  <th>Observed delivery success</th>
                  <th>Samples</th>
                  <th>Active obligations</th>
                  <th>Daily limit</th>
                </tr>
              </thead>
              <tbody>
                {report.numbers.map((n) => (
                  <tr key={n.id}>
                    <td>{n.phone}</td>
                    <td>{label(n.lifecycle)}</td>
                    <td title={n.health.reason}>{percent(n.health.score)}</td>
                    <td>{n.health.samples}</td>
                    <td>{n.active_obligations}</td>
                    <td>{n.daily_limit}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="fine">
            Health uses first-party delivery and adjusted answer evidence. A low
            pickup rate is not proof of a carrier spam label.
          </p>
          <p className="fine">
            Open a number’s evidence review in Numbers to verify callbacks or
            change workload.
          </p>
        </section>
      )}
      {!numbersOnly && report && (
        <>
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
            <details>
              <summary>Recent allocation and treatment decisions</summary>
              {report.decisions
                .slice(-8)
                .reverse()
                .map((decision) => (
                  <p className="fine" key={decision.id}>
                    {w.leads.find((l) => l.id === decision.lead_id)?.name ||
                      "Retained lead"}
                    :{" "}
                    {label(
                      decision.allocation?.reason || "authorized allocation",
                    )}
                    . {decision.allocation?.reassignment_reason} Treatment
                    probability {percent(decision.propensity)};{" "}
                    {label(decision.experiment_arm)}.
                  </p>
                ))}
            </details>
          </section>
          <section className="card padded optimization-panel">
            <h2>Operational training dataset</h2>
            <p>
              {report.dataset.rows} observed attempts ·{" "}
              {report.dataset.matured_rows} closed windows ·{" "}
              {report.dataset.pending_rows} pending outcomes ·{" "}
              {report.dataset.positive_conversions} observed conversions.
            </p>
            {report.dataset.missing_snapshot_calls > 0 && (
              <p className="fine">
                {report.dataset.missing_snapshot_calls} older attempts lack
                pre-call snapshots and are retained in history without inventing
                training features.
              </p>
            )}
            <p className="fine">
              Drift: {label(report.drift.action)}. Candidate models retain
              manual promotion and rollback controls below.
            </p>
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
                    Missing input coverage changed; check sources before
                    interpreting performance.
                  </p>
                )}
                {Object.entries(report.drift.stage_support).map(
                  ([stage, v]) => (
                    <p key={stage}>
                      {label(stage)}: {v.reference} reference / {v.recent}{" "}
                      recent classified outcomes; {v.paired_support} comparable
                      support. Brier{" "}
                      {v.reference_brier?.toFixed(4) ?? "unavailable"} →{" "}
                      {v.recent_brier?.toFixed(4) ?? "unavailable"}.
                    </p>
                  ),
                )}
              </details>
            )}
            {admin && (
              <div className="button-row">
                <button
                  className="secondary"
                  onClick={() =>
                    void run(async () => {
                      const result = await api<{
                        status: string;
                        rows: number;
                      }>("/v1/admin/optimization/train", "POST", {});
                      await load();
                      await refresh();
                      notice(
                        result.status === "insufficient_data"
                          ? "More independent labeled calls are required; " +
                              result.rows +
                              " rows available."
                          : "Training queued from the immutable call-history dataset",
                      );
                    })
                  }
                >
                  Train from call history
                </button>
                <button
                  className="secondary"
                  onClick={() =>
                    void run(async () => {
                      const saved = await api<{ id: string }>(
                        "/v1/admin/optimization/trial",
                        "POST",
                        {},
                      );
                      setCohortReportId(saved.id);
                      await load();
                      notice("Controlled cohort report saved for review");
                    })
                  }
                >
                  Generate trial report
                </button>
              </div>
            )}
            {cohortReportId && (
              <p className="fine">Saved cohort report: {cohortReportId}</p>
            )}
            <h3>Controlled conversion comparison</h3>
            <label>
              Cohort history
              <select
                value={report.trial.epoch}
                onChange={(e) =>
                  void run(async () => {
                    epoch.current = e.target.value;
                    setResponse(null);
                    await load();
                  })
                }
              >
                {report.cohort_history.map((epoch) => (
                  <option key={epoch} value={epoch}>
                    {epoch}
                  </option>
                ))}
              </select>
            </label>
            <div className="optimization-table">
              <table>
                <thead>
                  <tr>
                    <th>Group</th>
                    <th>Original leads</th>
                    <th>Matured leads</th>
                    <th>Conversions</th>
                    <th>Matured rate</th>
                    <th>95% interval</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(report.trial.arms).map(([arm, value]) => (
                    <tr key={arm}>
                      <td>{label(arm)}</td>
                      <td>{value.leads}</td>
                      <td>{value.matured_leads}</td>
                      <td>{value.conversions}</td>
                      <td>{percent(value.rate)}</td>
                      <td>
                        {percent(value.confidence_low)} –{" "}
                        {percent(value.confidence_high)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="fine">
              {report.trial.pending_leads} lead windows remain open. Planning
              requires approximately {report.trial.required_per_arm} matured
              leads per group for the configured example effect.{" "}
              {report.trial.sufficient_evidence
                ? "The observed comparison is ready for human review."
                : "There is not enough evidence to claim conversion improvement."}
            </p>
          </section>
          <TrialControls
            comparisons={report.comparisons}
            cohortReportId={cohortReportId}
            load={load}
          />
          <section className="card padded optimization-panel">
            <h2>Coaching quality and timing</h2>
            <p>
              {report.coaching.reviewed_cards} reviewed cards · Useful{" "}
              {percent(report.coaching.useful_fraction)} · Grounded{" "}
              {percent(report.coaching.grounded_fraction)} · Timely{" "}
              {percent(report.coaching.timely_fraction)}.
            </p>
            <p className="fine">
              p95 inference:{" "}
              {report.coaching.p95_inference_ms == null
                ? "No measurements"
                : report.coaching.p95_inference_ms.toFixed(0) + " ms"}
              . The 2.5-second full live guidance objective requires measured
              transcription, delivery and representative calls.
            </p>
            <p className="fine">
              State: {report.coaching.p95_state_ms?.toFixed(0) ?? "Unknown"} ms
              · Retrieval:{" "}
              {report.coaching.p95_retrieval_ms?.toFixed(0) ?? "Unknown"} ms ·
              Event receipt to render:{" "}
              {report.coaching.p95_event_to_render_ms?.toFixed(0) ?? "Unknown"}{" "}
              ms · Render:{" "}
              {report.coaching.p95_render_ms?.toFixed(0) ?? "Unknown"} ms. STT
              and full network delivery need live measurement.
            </p>
            {report.coaching.latency_quantization?.upper_bounds && (
              <p className="fine">
                Latency percentiles are upper estimates from bounded timing
                buckets; {report.coaching.latency_support?.inference_ms ?? 0}{" "}
                recorded inference timings. Missing measurements are excluded,
                not counted as zero.
              </p>
            )}
            {admin && (
              <details>
                <summary>Evaluate a reviewed conversation replay</summary>
                <label>
                  Reviewed replay cases JSON
                  <textarea
                    rows={5}
                    value={replay}
                    onChange={(e) => setReplay(e.target.value)}
                  />
                </label>
                <label>
                  Replay review reference
                  <input
                    value={replayReference}
                    onChange={(e) => setReplayReference(e.target.value)}
                  />
                </label>
                <button
                  className="secondary"
                  disabled={!replay || !replayReference}
                  onClick={() =>
                    void run(async () => {
                      const result = await api<{
                        samples: number;
                        attempted_samples: number;
                        unattempted_samples: number;
                        valid_fraction: number;
                        expected_action_fraction: number;
                        p95_inference_ms: number | null;
                        evidence_scope: string;
                      }>("/v1/admin/coaching/replay", "POST", {
                        cases: JSON.parse(replay),
                        review_reference: replayReference,
                      });
                      setReplayResult(
                        `${result.attempted_samples} of ${result.samples} cases attempted; ${result.unattempted_samples} not attempted. Schema and evidence checks: ` +
                          percent(result.valid_fraction) +
                          "; expected action " +
                          percent(result.expected_action_fraction) +
                          `. Attempt duration p95: ${result.p95_inference_ms?.toFixed(0) ?? "Unknown"} ms (includes failed attempts). Semantic usefulness has not been reviewed. ` +
                          result.evidence_scope,
                      );
                    })
                  }
                >
                  Evaluate reviewed replay
                </button>
                {replayResult && <p className="fine">{replayResult}</p>}
              </details>
            )}
          </section>
        </>
      )}
    </>
  );
}
