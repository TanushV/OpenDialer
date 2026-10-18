import { useCallback, useEffect, useRef, useState } from "react";
import { date, label, useWorkspace } from "../context";
import { TrialControls, type ControlledComparison } from "./TrialControls";
import { ConversationLearning } from "./ConversationLearning";

interface OptimizationSettings {
  conversion_goal: string;
  window_days: number;
  exploration: number;
  auto_train: boolean;
  horizon_hours: number;
  rep_minutes: number;
  handling_seconds: number;
  daily_capacity_per_number: number;
  training_min_rows: number;
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
    staffing_shortfall: number;
    number_shortfall: number;
    regions: {
      region: string;
      forecast_attempts: number;
      planned_attempts: number;
      usable_existing_capacity: number;
      general_fallback_capacity: number;
      capacity_waiting_for_review: number;
      proposed_additional: number;
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
    distracting_fraction: number | null;
    p95_inference_ms: number | null;
    p95_delivery_ms: number | null;
    p95_state_ms: number | null;
    p95_retrieval_ms: number | null;
    p95_event_to_render_ms: number | null;
    p95_render_ms: number | null;
  };
}
const percent = (value: number | null | undefined) =>
  value == null ? "Awaiting evidence" : (100 * value).toFixed(1) + "%";

export function OptimizationPanel({
  numbersOnly = false,
}: {
  numbersOnly?: boolean;
}) {
  const { api, run, notice, refresh, snapshot: w, subscribe } = useWorkspace();
  const [report, setReport] = useState<OptimizationReport | null>(null);
  const [settings, setSettings] = useState<OptimizationSettings | null>(null);
  const dirty = useRef(false);
  const [goalPreview, setGoalPreview] = useState<GoalPreview | null>(null);
  const [numberId, setNumberId] = useState("");
  const [reviewDecision, setReviewDecision] = useState("pause");
  const [reviewReference, setReviewReference] = useState("");
  const [reviewLimit, setReviewLimit] = useState("5");
  const [replay, setReplay] = useState("");
  const [replayReference, setReplayReference] = useState("");
  const [replayResult, setReplayResult] = useState<string | null>(null);
  const [cohortReportId, setCohortReportId] = useState("");
  const admin = ["owner", "admin"].includes(w.user.role);
  const load = useCallback(async () => {
    const next = await api<OptimizationReport>("/v1/optimization");
    setReport(next);
    if (!dirty.current) setSettings(next.settings);
    setNumberId((value) => value || next.numbers[0]?.id || "");
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
      if (pending) clearTimeout(pending);
    };
  }, [load, run, subscribe]);
  if (!report || !settings)
    return <p className="fine">Loading allocation and learning evidence…</p>;
  const change = <K extends keyof OptimizationSettings>(
    key: K,
    value: OptimizationSettings[K],
  ) => {
    dirty.current = true;
    setGoalPreview(null);
    setSettings({ ...settings, [key]: value });
  };
  const selected = report.numbers.find((n) => n.id === numberId);
  return (
    <>
      {!numbersOnly && <ConversationLearning />}
      {!numbersOnly && (
        <section className="card padded optimization-panel">
          <span className="eyebrow">PICKUP AND CONVERSION</span>
          <h2>Learn from the whole eligible cohort</h2>
          <p className="muted small">
            {report.metrics.unique_conversions} unique conversions /{" "}
            {report.metrics.original_eligible_leads} original eligible leads.{" "}
            {report.metrics.unique_right_party} people reached. Conversion rate:{" "}
            {percent(report.metrics.conversion_per_original_eligible_lead)}.
          </p>
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
              <label className="check-row">
                <input
                  type="checkbox"
                  checked={settings.auto_train}
                  onChange={(e) => change("auto_train", e.target.checked)}
                />
                Automatic training from call history
              </label>
              <div className="button-row">
                <button
                  className="secondary"
                  onClick={() =>
                    void run(async () => {
                      if (
                        settings.conversion_goal !==
                          report.settings.conversion_goal ||
                        settings.window_days !== report.settings.window_days
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
                      notice("Learning and capacity refreshed");
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
      <section className="card padded optimization-panel">
        <h2>Capacity before procurement</h2>
        <p className="muted small">
          Forecast includes due attempts, usable inventory and available rep
          time. New rentals are blocked; proposed additions are shared-capacity
          scenarios, not one number per area code.
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
                  <td>{region.proposed_additional}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {report.forecast.capacity_shortfall > 0 && (
          <p className="fine">
            {report.forecast.staffing_shortfall} attempts exceed declared rep
            time; {report.forecast.number_shortfall} staffed attempts lack
            usable number capacity. These are separate constraints.
          </p>
        )}
        <h3>Number evidence and workload</h3>
        <div className="optimization-table">
          <table>
            <thead>
              <tr>
                <th>Number</th>
                <th>State</th>
                <th>Observed health</th>
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
        {numbersOnly && admin && (
          <details>
            <summary>Review number workload</summary>
            <div className="optimization-fields">
              <label>
                Number to review
                <select
                  value={numberId}
                  onChange={(e) => {
                    setNumberId(e.target.value);
                    setReviewLimit(
                      String(
                        report.numbers.find((n) => n.id === e.target.value)
                          ?.daily_limit || 5,
                      ),
                    );
                  }}
                >
                  {report.numbers.map((n) => (
                    <option key={n.id} value={n.id}>
                      {n.phone}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                Number review decision
                <select
                  value={reviewDecision}
                  onChange={(e) => setReviewDecision(e.target.value)}
                >
                  <option value="pause">
                    Pause outbound; retain callbacks
                  </option>
                  <option value="reduce">Reduce workload</option>
                  <option value="increase">Increase after observed use</option>
                  <option value="resume">Resume limited use</option>
                </select>
              </label>
              <label>
                Reviewed daily limit
                <input
                  type="number"
                  min={1}
                  value={reviewLimit}
                  onChange={(e) => setReviewLimit(e.target.value)}
                />
              </label>
              <label>
                Number review reference
                <input
                  value={reviewReference}
                  onChange={(e) => setReviewReference(e.target.value)}
                />
              </label>
            </div>
            <button
              className="secondary"
              disabled={!selected || !reviewReference}
              onClick={() =>
                void run(async () => {
                  await api(
                    "/v1/admin/numbers/" + numberId + "/review",
                    "POST",
                    {
                      decision: reviewDecision,
                      review_reference: reviewReference,
                      daily_limit: Number(reviewLimit),
                    },
                  );
                  await load();
                  await refresh();
                  notice(
                    "Number workload reviewed; inbound callbacks retained",
                  );
                })
              }
            >
              Apply number review
            </button>
          </details>
        )}
      </section>
      {!numbersOnly && (
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
                    const next = await api<OptimizationReport>(
                      "/v1/optimization?epoch=" +
                        encodeURIComponent(e.target.value),
                    );
                    setReport(next);
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
                        valid_fraction: number;
                        expected_action_fraction: number;
                        evidence_scope: string;
                      }>("/v1/admin/coaching/replay", "POST", {
                        cases: JSON.parse(replay),
                        review_reference: replayReference,
                      });
                      setReplayResult(
                        "Valid " +
                          percent(result.valid_fraction) +
                          "; expected action " +
                          percent(result.expected_action_fraction) +
                          ". " +
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
