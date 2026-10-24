import { useEffect, useRef, useState } from "react";
import { useHistory } from "../state/useHistory";
import { limits, useDefinitions } from "../state/definitions";
import { HistoryMore } from "./HistoryMore";
import { date, label, percent, useWorkspace } from "../context";
import type { ReportState } from "./OptimizationPanel";

export interface ControlledComparison {
  id: string;
  family: string;
  ends_at: string;
  stopped_at?: string;
  strategy_ids?: string[];
  strategy_policy?: "two_arm" | "adaptive";
  strategy_exposure?: {
    generated: number;
    displayed: number;
    reported_used: number;
    reported_not_used: number;
    use_unknown: number;
    observed_in_speech: number;
  };
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

type Kind = "model_pilot" | "approach" | "coaching" | "number_allocation";
type ExperimentKind = Kind;
const KINDS: [ExperimentKind, string][] = [
  ["model_pilot", "Reviewed model pilot"],
  ["approach", "Response approach comparison"],
  ["coaching", "Live help availability (coaching)"],
  ["number_allocation", "Regional number allocation"],
];
const START: Record<ExperimentKind, string> = {
  model_pilot: "Start bounded model pilot",
  approach: "Start reviewed strategy comparison",
  coaching: "Start coaching comparison",
  number_allocation: "Start number comparison",
};

/** One experiment surface: model pilots, approach, help and number comparisons. Script wording is tested from Script memory. */
export function Experiments({ state }: { state: ReportState }) {
  const { api, run, refresh, notice, snapshot: w } = useWorkspace();
  const bounds = useDefinitions();
  const trial = bounds?.model_trial;
  const comparison = bounds?.comparison;
  const history = useHistory("models", w.models);
  const report = state.report;
  const comparisons = report?.comparisons ?? [];
  const [kind, setKind] = useState<ExperimentKind>("model_pilot");
  const [reference, setReference] = useState("");
  const [days, setDays] = useState<number | null>(null);
  const [limit, setLimit] = useState<number | null>(null);
  const [probability, setProbability] = useState<number | null>(null);
  const [modelId, setModelId] = useState("");
  const [reason, setReason] = useState("");
  const [cohortReport, setCohortReport] = useState("");
  const [approaches, setApproaches] = useState<
    { id: string; source_version: number; text: string }[]
  >([]);
  const [approachId, setApproachId] = useState("");
  const [policy, setPolicy] = useState<"two_arm" | "adaptive">("two_arm");
  const strategyRequest = useRef<{
    fingerprint: string;
    requestId: string;
  } | null>(null);
  useEffect(() => {
    let live = true;
    void api<{ strategies: typeof approaches }>("/v1/optimization/strategies")
      .then((value) => {
        if (live)
          setApproaches(value.strategies.filter((a) => a.id !== "control"));
      })
      .catch(() => {
        if (live) setApproaches([]);
      });
    return () => {
      live = false;
    };
  }, [api]);
  // Defaults come from the authority's definitions until the admin edits them.
  const durationBound = (kind === "model_pilot" ? trial : comparison)
    ?.duration_days;
  const duration = days ?? Number(durationBound?.default ?? Number.NaN);
  const leads = limit ?? Number(trial?.max_leads?.default ?? Number.NaN);
  const share =
    probability ??
    Number(comparison?.candidate_probability?.default ?? Number.NaN);
  const selected = history.items.find((model) => model.id === modelId);
  const supportedStudy = comparisons.find(
    (s) =>
      s.strategy_policy === "two_arm" &&
      s.sufficient_evidence &&
      s.strategy_ids?.length === 2 &&
      s.strategy_ids[1] === approachId,
  );
  const changed = async () => {
    await state.load();
    await refresh();
  };
  const reviewed = reference.trim().length >= 3;
  const blocked = !bounds
    ? "Loading experiment limits…"
    : kind === "model_pilot" && !modelId
      ? "Choose an owned candidate."
      : kind === "approach" && !approaches.length
        ? "Publish a source-linked coaching example from call review before starting an approach trial."
        : kind === "approach" && !approachId
          ? "Choose a reviewed teaching example."
          : !reviewed
            ? "Enter a review reference of at least 3 characters."
            : kind === "model_pilot" &&
                selected?.feature_schema_version === 2 &&
                !selected.reviewable
              ? "This candidate's predictive evidence is not reviewable, so it cannot start a pilot."
              : kind === "approach" && policy === "adaptive" && !supportedStudy
                ? "Adaptive allocation requires a supported completed comparison for this approach."
                : "";
  async function start() {
    if (kind === "model_pilot") {
      await api("/v1/admin/models/" + modelId + "/start-trial", "POST", {
        review_reference: reference,
        max_leads: leads,
        duration_days: duration,
      });
      notice("Reviewed bounded model pilot started");
    } else if (kind === "approach") {
      const body = {
        family: "coaching",
        review_reference: reference,
        duration_days: duration,
        strategy_ids: ["control", approachId],
        strategy_policy: policy,
        candidate_probability: share,
        evidence_comparison_id: supportedStudy?.id,
      };
      const fingerprint = JSON.stringify(body);
      if (strategyRequest.current?.fingerprint !== fingerprint)
        strategyRequest.current = {
          fingerprint,
          requestId: crypto.randomUUID(),
        };
      await api("/v1/admin/optimization/comparisons", "POST", {
        ...body,
        request_id: strategyRequest.current.requestId,
      });
      strategyRequest.current = null;
      notice("Reviewed approach comparison started");
    } else {
      await api("/v1/admin/optimization/comparisons", "POST", {
        family: kind,
        review_reference: reference,
        duration_days: duration,
      });
      notice("Reviewed comparison started");
    }
    await changed();
  }
  return (
    <section
      className="card padded optimization-panel"
      aria-label="Experiments"
    >
      <h2>Experiments</h2>
      <p className="fine">
        Every comparison keeps a fixed control, logs exact assignment
        probabilities and needs human review. Calling permissions remain
        authoritative; wording stays freeform. Predictive validation does not
        establish conversion lift.
      </p>
      <div className="optimization-fields">
        <label>
          Experiment type
          <select
            value={kind}
            onChange={(e) => setKind(e.target.value as ExperimentKind)}
          >
            {KINDS.map(([value, title]) => (
              <option key={value} value={value}>
                {title}
              </option>
            ))}
          </select>
        </label>
        <label>
          Experiment review reference
          <input
            value={reference}
            onChange={(e) => setReference(e.target.value)}
          />
        </label>
        <label>
          Experiment duration (days)
          <input
            type="number"
            {...limits(durationBound)}
            value={Number.isFinite(duration) ? duration : ""}
            onChange={(e) => setDays(Number(e.target.value))}
          />
        </label>
        {kind === "model_pilot" && (
          <label>
            Pilot maximum leads
            <input
              type="number"
              {...limits(trial?.max_leads)}
              value={Number.isFinite(leads) ? leads : ""}
              onChange={(e) => setLimit(Number(e.target.value))}
            />
          </label>
        )}
      </div>
      {kind === "model_pilot" && (
        <>
          <label>
            Pilot candidate
            <select
              value={modelId}
              onChange={(e) => setModelId(e.target.value)}
            >
              <option value="">Choose an owned candidate</option>
              {history.items.map((model) => (
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
          <HistoryMore history={history} label="Load more pilot candidates" />
          {selected && <CandidateEvidence model={selected} />}
        </>
      )}
      {kind === "approach" && (
        <>
          <label>
            Reviewed response approach
            <select
              value={approachId}
              onChange={(e) => setApproachId(e.target.value)}
            >
              <option value="">Choose a reviewed teaching example</option>
              {approaches.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.text}
                </option>
              ))}
            </select>
          </label>
          <p className="fine">
            Wording stays freeform. Control receives ordinary contextual help.
          </p>
          <label>
            Strategy allocation
            <select
              value={policy}
              onChange={(e) =>
                setPolicy(e.target.value as "two_arm" | "adaptive")
              }
            >
              <option value="two_arm">Fixed control and approach</option>
              <option value="adaptive">
                90% supported best + 10% exploration
              </option>
            </select>
          </label>
          {policy === "two_arm" && (
            <label>
              Approach assignment probability
              <input
                type="number"
                step={0.05}
                {...limits(comparison?.candidate_probability)}
                value={Number.isFinite(share) ? share : ""}
                onChange={(e) => setProbability(Number(e.target.value))}
              />
            </label>
          )}
        </>
      )}
      {(kind === "coaching" || kind === "number_allocation") && (
        <p className="fine">
          {kind === "coaching"
            ? "Randomized two-lead blocks within each rep compare approved static help with the configured live helper."
            : "Regional numbers use fixed region clusters and preserve existing caller-ID bindings."}
        </p>
      )}
      <p className="fine">
        Script wording is tested from Script memory: a proposed change runs
        against the base script and replaces it only if it wins.
      </p>
      <button
        className="secondary"
        disabled={!!blocked}
        onClick={() => void run(start)}
      >
        {START[kind]}
      </button>
      {blocked && (
        <p className="fine" role="status">
          {blocked}
        </p>
      )}
      {kind === "model_pilot" && (
        <details>
          <summary>Train, stop, roll back or promote a candidate</summary>
          <div className="button-row">
            <button
              className="secondary"
              onClick={() =>
                void run(async () => {
                  const result = await api<{ status: string; rows: number }>(
                    "/v1/admin/optimization/train",
                    "POST",
                    {},
                  );
                  await changed();
                  notice(
                    result.status === "insufficient_data"
                      ? `More independent labeled calls are required; ${result.rows} rows available.`
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
                  setCohortReport(saved.id);
                  await state.load();
                  notice("Controlled cohort report saved for review");
                })
              }
            >
              Generate trial report
            </button>
          </div>
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
          <label>
            Saved cohort report ID
            <input
              value={cohortReport}
              onChange={(e) => setCohortReport(e.target.value)}
            />
          </label>
          {cohortReport && (
            <p className="fine">Saved cohort report: {cohortReport}</p>
          )}
          <p className="fine">
            The authority reads trial outcome, coverage and policy evidence from
            the saved report; activation re-checks every gate.
          </p>
          <div className="button-row">
            <button
              className="secondary"
              disabled={!modelId || !reviewed || !cohortReport.trim()}
              onClick={() =>
                void run(async () => {
                  await api("/v1/admin/models/" + modelId + "/trial", "POST", {
                    report_reference: reference,
                    cohort_report_id: cohortReport.trim(),
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
                    {},
                  );
                  await changed();
                  notice("Reviewed model activated");
                })
              }
            >
              Activate after gates
            </button>
          </div>
          {(!modelId || !reviewed || !cohortReport.trim()) && (
            <p className="fine">
              Choose a candidate, enter a review reference and name a saved
              cohort report to review it.
            </p>
          )}
        </details>
      )}
      {report && (
        <>
          <h3>Controlled conversion comparison</h3>
          <p className="fine">
            {report.metrics.unique_conversions} unique conversions /{" "}
            {report.metrics.original_eligible_leads} original eligible leads in
            the selected cohort.
          </p>
          <label>
            Cohort history
            <select
              value={report.trial.epoch}
              onChange={(e) =>
                void run(() => state.selectEpoch(e.target.value))
              }
            >
              {report.cohort_history.map((epoch) => (
                <option key={epoch} value={epoch}>
                  {epoch}
                </option>
              ))}
            </select>
          </label>
          <ArmTable arms={report.trial.arms} />
          <p className="fine">
            {report.trial.pending_leads} lead windows remain open. Planning
            requires approximately {report.trial.required_per_arm} matured leads
            per group for the configured example effect.{" "}
            {report.trial.sufficient_evidence
              ? "The observed comparison is ready for human review."
              : "There is not enough evidence to claim conversion improvement."}
          </p>
        </>
      )}
      {comparisons.map((study) => (
        <div className="comparison-summary" key={study.id}>
          <h3>
            {study.strategy_ids
              ? "Reviewed approach comparison"
              : study.family === "coaching"
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
          {study.strategy_exposure && (
            <p className="fine">
              {study.strategy_exposure.generated} generated ·{" "}
              {study.strategy_exposure.displayed} displayed ·{" "}
              {study.strategy_exposure.reported_used} reported used ·{" "}
              {study.strategy_exposure.reported_not_used} not used ·{" "}
              {study.strategy_exposure.use_unknown} use unknown ·{" "}
              {study.strategy_exposure.observed_in_speech ?? 0} reviewed speech
              matches. These are not conversion labels.
            </p>
          )}
          <ArmTable arms={study.arms} />
          {!study.stopped_at && (
            <button
              className="text-button"
              onClick={() =>
                void run(async () => {
                  await api(
                    "/v1/admin/optimization/comparisons/" + study.id + "/stop",
                    "POST",
                    {
                      reason:
                        reference || "Operator stopped the reviewed comparison",
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

function ArmTable({ arms }: { arms: ControlledComparison["arms"] }) {
  return (
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
          {Object.entries(arms).map(([arm, value]) => (
            <tr key={arm}>
              <td>{label(arm)}</td>
              <td>{value.leads}</td>
              <td>{value.matured_leads}</td>
              <td>{value.conversions}</td>
              <td>
                {percent(value.confidence_low)} –{" "}
                {percent(value.confidence_high)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Reviewable evidence summary for one candidate; raw calibration tables stay in the record. */
function CandidateEvidence({
  model,
}: {
  model: ReturnType<typeof useWorkspace>["snapshot"]["models"][number];
}) {
  return (
    <>
      <p className="fine">
        Goal {label(model.goal || "legacy")}
        {model.trial_ends_at
          ? " · Pilot ends " + date(model.trial_ends_at)
          : ""}
      </p>
      {model.feature_schema_version === 2 && (
        <>
          <p role="status">
            Predictive evidence:{" "}
            {model.reviewable ? "reviewable" : "not reviewable"}
          </p>
          <details>
            <summary>Model evaluation</summary>
            <p>
              Objective: {label(model.objective_stage || "unknown")} ·{" "}
              {model.window_days ?? "Unknown"}-day outcome window.
            </p>
            <p>
              Linked evaluation entities: {model.bootstrap?.groups ?? 0}. Paired
              difference interval:{" "}
              {model.bootstrap?.interval
                ?.map((v) => v.toFixed(4))
                .join(" to ") || "Unavailable"}
              .
            </p>
            <p>
              Pooled stages:{" "}
              {model.pooled_stages?.map(label).join(", ") || "None reported"}.
            </p>
            <p>
              Conversation features:{" "}
              {model.conversation_ablation?.eligible_for_serving
                ? "retained after held-out comparison"
                : "excluded after held-out comparison"}
            </p>
            <p className="fine">
              Reviewed live cohort evidence is required before promotion.
            </p>
          </details>
        </>
      )}
    </>
  );
}
