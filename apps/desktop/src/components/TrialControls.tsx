import { useState } from "react";
import { date, label, useWorkspace } from "../context";

export interface ControlledComparison {
  id: string;
  family: string;
  ends_at: string;
  stopped_at?: string;
  original_eligible_leads: number;
  assigned_leads: number;
  unassigned_leads: number;
  cluster_count: number;
  sufficient_evidence: boolean;
  inference_unit: string;
  rep_strata: unknown[];
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
}

export function TrialControls({
  comparisons,
  cohortReportId,
  load,
}: {
  comparisons: ControlledComparison[];
  cohortReportId: string;
  load: () => Promise<void>;
}) {
  const { api, run, refresh, notice, snapshot: w } = useWorkspace();
  const [modelId, setModelId] = useState("");
  const [reference, setReference] = useState("");
  const [comparisonReference, setComparisonReference] = useState("");
  const [reason, setReason] = useState("");
  const [days, setDays] = useState(14);
  const [limit, setLimit] = useState(200);
  const [savedReport, setSavedReport] = useState("");
  const admin = ["owner", "admin"].includes(w.user.role);
  const selected = w.models.find((model) => model.id === modelId);
  const changed = async () => {
    await load();
    await refresh();
  };
  return (
    <section className="card padded optimization-panel">
      <h2>Reviewed model pilot</h2>
      <p className="fine">
        A bounded candidate serves enrolled leads with a fixed baseline control.
        Promotion requires a saved live cohort report, closed outcome windows,
        adequate evidence, preserved coverage and human review.
      </p>
      {admin && (
        <>
          <label>
            Pilot candidate
            <select
              value={modelId}
              onChange={(e) => setModelId(e.target.value)}
            >
              <option value="">Choose an owned candidate</option>
              {w.models.map((model) => (
                <option value={model.id} key={model.id}>
                  {model.id.slice(0, 12)} ·{" "}
                  {model.active
                    ? "Active"
                    : model.trial_active
                      ? "Pilot"
                      : "Candidate"}
                </option>
              ))}
            </select>
          </label>
          {selected && (
            <p className="fine">
              Human-answer Brier {selected.metrics.brier?.toFixed(4)} · Pooled
              baseline {selected.metrics.pooled_baseline_brier?.toFixed(4)} ·
              Goal {label(selected.goal || "legacy")}
              {selected.trial_ends_at
                ? " · Pilot ends " + date(selected.trial_ends_at)
                : ""}
            </p>
          )}
          <div className="optimization-fields">
            <label>
              Pilot review reference
              <input
                value={reference}
                onChange={(e) => setReference(e.target.value)}
              />
            </label>
            <label>
              Pilot maximum leads
              <input
                type="number"
                min={20}
                max={10000}
                value={limit}
                onChange={(e) => setLimit(Number(e.target.value))}
              />
            </label>
            <label>
              Pilot duration (days)
              <input
                type="number"
                min={1}
                max={90}
                value={days}
                onChange={(e) => setDays(Number(e.target.value))}
              />
            </label>
          </div>
          <button
            className="secondary"
            disabled={!modelId || reference.trim().length < 3}
            onClick={() =>
              void run(async () => {
                await api(
                  "/v1/admin/models/" + modelId + "/start-trial",
                  "POST",
                  {
                    review_reference: reference,
                    max_leads: limit,
                    duration_days: days,
                  },
                );
                await changed();
                notice("Reviewed bounded model pilot started");
              })
            }
          >
            Start bounded model pilot
          </button>
          <label>
            Pilot stop or rollback reason
            <input value={reason} onChange={(e) => setReason(e.target.value)} />
          </label>
          <div className="button-row">
            <button
              className="secondary"
              disabled={!modelId || reason.trim().length < 3}
              onClick={() =>
                void run(async () => {
                  await api(
                    "/v1/admin/models/" + modelId + "/stop-trial",
                    "POST",
                    { reason },
                  );
                  await changed();
                  notice("Model pilot stopped");
                })
              }
            >
              Stop model pilot
            </button>
            <button
              className="secondary"
              disabled={reason.trim().length < 3}
              onClick={() =>
                void run(async () => {
                  await api("/v1/admin/models/baseline/rollback", "POST", {
                    reason,
                  });
                  await changed();
                  notice("Baseline restored");
                })
              }
            >
              Restore pooled baseline
            </button>
          </div>
          <details>
            <summary>Review a completed cohort and promote</summary>
            <label>
              Saved cohort report ID
              <input
                value={savedReport || cohortReportId}
                onChange={(e) => setSavedReport(e.target.value)}
              />
            </label>
            <button
              className="secondary"
              disabled={
                !modelId ||
                reference.trim().length < 3 ||
                !(savedReport || cohortReportId)
              }
              onClick={() =>
                void run(async () => {
                  await api("/v1/admin/models/" + modelId + "/trial", "POST", {
                    report_reference: reference,
                    cohort_report_id: savedReport || cohortReportId,
                    online_trial_passed: true,
                    coverage_delta: 0,
                    policy_violations: 0,
                  });
                  await changed();
                  notice("Saved cohort evidence reviewed");
                })
              }
            >
              Review saved cohort evidence
            </button>
            <button
              className="primary"
              disabled={!modelId}
              onClick={() =>
                void run(async () => {
                  await api(
                    "/v1/admin/models/" + modelId + "/activate",
                    "POST",
                    { approved: true },
                  );
                  await changed();
                  notice("Reviewed model activated");
                })
              }
            >
              Activate after gates
            </button>
          </details>
          <h3>Controlled number and coaching comparisons</h3>
          <label>
            Comparison review reference
            <input
              value={comparisonReference}
              onChange={(e) => setComparisonReference(e.target.value)}
            />
          </label>
          <p className="fine">
            Regional numbers use fixed region clusters and preserve existing
            bindings. Coaching uses randomized two-lead blocks within each rep,
            comparing approved static help with the configured coach. Existing
            call permissions remain authoritative.
          </p>
          <div className="button-row">
            {["number_allocation", "coaching"].map((family) => (
              <button
                className="secondary"
                key={family}
                disabled={comparisonReference.trim().length < 3}
                onClick={() =>
                  void run(async () => {
                    await api("/v1/admin/optimization/comparisons", "POST", {
                      family,
                      review_reference: comparisonReference,
                      duration_days: days,
                    });
                    await changed();
                    notice("Reviewed comparison started");
                  })
                }
              >
                {family === "coaching"
                  ? "Start coaching comparison"
                  : "Start number comparison"}
              </button>
            ))}
          </div>
        </>
      )}
      {selected?.calibration && (
        <details>
          <summary>Calibration and subgroup evidence</summary>
          <p className="fine">
            Expected calibration error:{" "}
            {selected.calibration.expected_calibration_error?.toFixed(4)}. Small
            groups are descriptive; rows are held out chronologically.
          </p>
          <div className="optimization-table">
            <table>
              <thead>
                <tr>
                  <th>Predicted bin</th>
                  <th>Samples</th>
                  <th>Mean prediction</th>
                  <th>Observed pickup</th>
                </tr>
              </thead>
              <tbody>
                {selected.calibration.bins.map((bin) => (
                  <tr key={bin.lower}>
                    <td>
                      {bin.lower.toFixed(1)}–{bin.upper.toFixed(1)}
                    </td>
                    <td>{bin.samples}</td>
                    <td>{bin.mean_prediction.toFixed(3)}</td>
                    <td>{bin.observed_rate.toFixed(3)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {Object.entries(selected.calibration.subgroups).map(
            ([dimension, groups]) => (
              <div key={dimension}>
                <h4>{label(dimension)}</h4>
                {Object.entries(groups)
                  .slice(0, 12)
                  .map(([group, value]) => (
                    <p className="fine" key={group}>
                      {group.slice(0, 18)} · {value.samples} samples · Brier{" "}
                      {value.brier.toFixed(4)} ·{" "}
                      {value.seen_during_training
                        ? "Group observed in training"
                        : "Previously unseen group"}
                    </p>
                  ))}
              </div>
            ),
          )}
        </details>
      )}
      {comparisons.map((study) => (
        <div className="comparison-summary" key={study.id}>
          <h3>
            {study.family === "coaching"
              ? "Rep-balanced coaching"
              : "Regional number comparison"}
          </h3>
          <p>
            {study.original_eligible_leads} original eligible leads ·{" "}
            {study.assigned_leads} assigned · {study.unassigned_leads} awaiting
            assignment · {study.cluster_count || study.rep_strata.length}{" "}
            {study.family === "coaching" ? "rep strata" : "region clusters"}.
          </p>
          <p className="fine">
            {study.inference_unit}.{" "}
            {study.stopped_at
              ? "Stopped"
              : "Enrollment ends " + date(study.ends_at)}
            .{" "}
            {study.sufficient_evidence
              ? "Ready for reviewed interpretation"
              : "Insufficient evidence for outcome lift"}
            .
          </p>
          <div className="optimization-table">
            <table>
              <thead>
                <tr>
                  <th>Group</th>
                  <th>Leads</th>
                  <th>Closed windows</th>
                  <th>Conversions</th>
                  <th>95% interval</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(study.arms).map(([arm, value]) => (
                  <tr key={arm}>
                    <td>{label(arm)}</td>
                    <td>{value.leads}</td>
                    <td>{value.matured_leads}</td>
                    <td>{value.conversions}</td>
                    <td>
                      {(100 * value.confidence_low).toFixed(1)}% –{" "}
                      {(100 * value.confidence_high).toFixed(1)}%
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {admin && !study.stopped_at && (
            <button
              className="text-button"
              onClick={() =>
                void run(async () => {
                  await api(
                    "/v1/admin/optimization/comparisons/" + study.id + "/stop",
                    "POST",
                    {
                      reason:
                        comparisonReference ||
                        "Operator stopped the reviewed comparison",
                    },
                  );
                  await changed();
                  notice("Controlled comparison stopped");
                })
              }
            >
              {study.family === "coaching"
                ? "Stop coaching comparison"
                : "Stop number comparison"}
            </button>
          )}
        </div>
      ))}
    </section>
  );
}
