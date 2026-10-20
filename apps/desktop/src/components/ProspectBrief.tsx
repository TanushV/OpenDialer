import { useEffect, useState } from "react";
import { date, label, useWorkspace } from "../context";
import type { NumberRecord, Packet } from "../types";
import { localEdit } from "../api/client";
import {
  chooseZone,
  localTime,
  zoneCaption,
  zonedInput,
  zonedInstant,
} from "../state/zones";

type Stats = NonNullable<Packet["statistical_guidance"]>;
const pct = (value: number) => `${Math.round(value * 100)}%`;

/** Compact stage estimates with their support; never a bare percentage for a prior. */
function stageEstimates(stats: Stats) {
  if (stats.prior_only)
    return "Estimates: prior only · no observed outcomes yet";
  return (
    [
      ["human", "Pickup", stats.human_probability, stats.samples],
      ["right_party", "Right party", stats.right_party_given_human, undefined],
      [
        "conversion",
        label(stats.conversion_goal),
        stats.conversion_given_right_party,
        stats.conversion_samples,
      ],
    ] as const
  )
    .map(([stage, title, value, samples]) => {
      const support = stats.stage_support?.[stage];
      const n = support?.cell_known ?? samples;
      if (value == null || !Number.isFinite(value)) return `${title}: unknown`;
      if (support && !support.cell_known && !support.outside_known)
        return `${title}: prior only`;
      return `${title} ${pct(value)}${n == null ? "" : ` (n ${n})`}`;
    })
    .join(" · ");
}

export function ProspectBrief({
  packet,
  onChange,
  number,
}: {
  packet: Packet;
  onChange: (packet: Packet) => void;
  /** The calling identity selected for this lead, when known. */
  number?: Pick<NumberRecord, "phone" | "region"> | null;
}) {
  const { api, run, refresh, notice } = useWorkspace();
  const brief = packet.prospect_brief;
  const stats = packet.statistical_guidance;
  const zone = chooseZone(packet.lead.timezone, brief?.meeting?.timezone);
  const [status, setStatus] = useState(brief?.meeting?.status || "booked");
  const [next, setNext] = useState("");
  const [when, setWhen] = useState(
    zonedInput(brief?.meeting?.due_at, zone.zone),
  );
  const [callback, setCallback] = useState("");
  const [reviewAction, setReviewAction] = useState("resume");
  const [reviewReason, setReviewReason] = useState("");
  const [reviewTime, setReviewTime] = useState("");
  useEffect(() => {
    if (brief?.meeting?.status === "needs_review") {
      setStatus("needs_review");
      setNext("");
    }
  }, [brief?.meeting?.status]);
  const prospectTime = localTime(packet.lead.timezone);
  return (
    <div className="prospect-context">
      <div data-testid="statistical-guidance" className="small">
        <strong>Calling context</strong>
        {stats ? (
          <>
            <p>{stageEstimates(stats)}</p>
            <p className="muted">Why now: {stats.why_now}</p>
            <details>
              <summary>Estimates &amp; uncertainty</summary>
              <p>
                {stats.source === "frozen_call_decision" ||
                stats.source === "frozen_reservation_decision"
                  ? "Estimates frozen at this selection."
                  : "Current pooled estimate for a future selection."}
              </p>
              <p>{stats.uncertainty}</p>
              {stats.stage_support && (
                <>
                  <p>
                    {stats.estimate_basis === "reviewed_model"
                      ? "Reviewed model estimate; counts below describe the pooled baseline."
                      : stats.prior_only
                        ? "Starting priors; no observed support yet."
                        : `Estimate level: ${label(stats.estimate_level || "pooled")}.`}
                  </p>
                  <table aria-label="Conditional estimate support">
                    <thead>
                      <tr>
                        <th>Stage</th>
                        <th>Classified / unknown</th>
                        <th>Pooled support</th>
                        <th>Observed interval</th>
                      </tr>
                    </thead>
                    <tbody>
                      {Object.entries(stats.stage_support).map(
                        ([stage, support]) => (
                          <tr key={stage}>
                            <td>{label(stage)}</td>
                            <td>
                              {support.cell_known} / {support.cell_unknown}
                            </td>
                            <td>{support.outside_known}</td>
                            <td>
                              {support.observed_interval.map(pct).join("–")}
                            </td>
                          </tr>
                        ),
                      )}
                    </tbody>
                  </table>
                  <p>
                    Observed-rate intervals describe classified history, not
                    certainty about this prospect.
                  </p>
                  {stats.estimate_policy && (
                    <p>
                      Prior success/failure weights: pickup{" "}
                      {stats.estimate_policy.human_prior.join("/")}, right party{" "}
                      {stats.estimate_policy.right_prior.join("/")}, selected
                      goal {stats.estimate_policy.goal_prior.join("/")}. Work
                      assumption {stats.work_assumption_seconds} seconds;{" "}
                      {stats.estimate_policy.work_prior} prior attempt
                      equivalents.
                    </p>
                  )}
                </>
              )}
              <p>
                {stats.as_of
                  ? `Updated ${date(stats.as_of)}`
                  : "Pooled starting assumptions; no measured outcome estimate yet."}
              </p>
            </details>
          </>
        ) : (
          <p className="muted">Estimates: unknown for this lead.</p>
        )}
      </div>
      <div data-testid="prospect-brief" className="small">
        <p>
          Local time:{" "}
          {prospectTime ?? "unknown · the prospect's time zone needs review"}
        </p>
        {number && (
          <p>
            Calling from {number.phone} ·{" "}
            {number.region && number.region === packet.lead.region
              ? `local to the prospect's area (${number.region})`
              : `region ${number.region || "unknown"}`}
          </p>
        )}
        <p>
          Last outcome:{" "}
          {brief?.last_outcome ? label(brief.last_outcome) : "none yet"}
        </p>
        {brief?.needs.map((n, i) => (
          <p key={`need-${i}`}>Needs: {n.text}</p>
        ))}
        {packet.commitments.map((c, i) => (
          <p key={`promise-${i}`}>
            Promised: {c.action} · {date(c.due_at, zone.zone)}
          </p>
        ))}
        {brief?.next_question && <p>Next question: {brief.next_question}</p>}
        <details>
          <summary>Prior conversation &amp; next step</summary>
          {packet.context_coverage && (
            <p className="muted">
              Compact view: {packet.context_coverage.calls_omitted} earlier
              calls, {packet.context_coverage.commitments_omitted} other
              commitments and {packet.context_coverage.tasks_omitted} other
              tasks omitted. Unresolved work is shown first; retained history is
              unchanged.
            </p>
          )}
          <p>{brief?.summary || "No previous interpreted conversation."}</p>
          {brief?.freshness && (
            <p>Preparation: {label(brief.freshness.state)}</p>
          )}
          {brief?.interpretation_gaps?.map((gap) => (
            <p key={gap.call_id} role="status">
              Earlier rundown: {label(gap.status)}.{" "}
              {gap.reason
                ? label(gap.reason)
                : "Some conversation context is unavailable."}
            </p>
          ))}
          {brief?.prior_answers?.map((answer, index) => (
            <p key={`answer-${index}`}>Previously answered: {answer.text}</p>
          ))}
          {brief?.unanswered_questions?.map((question, index) => (
            <p key={`question-${index}`}>Open question: {question.text}</p>
          ))}
          {brief?.human_notes.map((n) => (
            <p key={n.call_id}>Rep note: {n.text}</p>
          ))}
          {brief?.unresolved.map((n, i) => (
            <p key={`issue-${i}`}>Unresolved: {n.text}</p>
          ))}
          {brief?.observations?.map((item) => (
            <p key={item.id}>
              Observed topic: {item.concept}
              {item.approach
                ? ` · Prior response approach: ${item.approach}`
                : ""}{" "}
              ({label(item.review_status)})
            </p>
          ))}
          {packet.lead.outreach_hold && (
            <p role="status">
              Outreach paused: {label(packet.lead.outreach_hold)}
            </p>
          )}
          {packet.lead.outreach_hold === "goal_changed" && (
            <section aria-label="Review next outreach">
              <p>
                The campaign goal changed. Review the relationship and choose
                the next action.
              </p>
              <label>
                Goal review next action
                <select
                  aria-label="Goal review next action"
                  value={reviewAction}
                  onChange={(e) => setReviewAction(e.target.value)}
                >
                  <option value="resume">Resume when policy allows</option>
                  <option value="callback">Confirmed callback</option>
                </select>
              </label>
              <label>
                Goal review reason
                <input
                  aria-label="Goal review reason"
                  value={reviewReason}
                  onChange={(e) => setReviewReason(e.target.value)}
                />
              </label>
              {reviewAction === "callback" && (
                <label>
                  Reviewed callback time ({zoneCaption(zone)})
                  <input
                    type="datetime-local"
                    value={reviewTime}
                    onChange={(e) => setReviewTime(e.target.value)}
                  />
                </label>
              )}
              <button
                className="small secondary"
                disabled={
                  reviewReason.trim().length < 3 ||
                  (reviewAction === "callback" && !reviewTime)
                }
                onClick={() =>
                  void run(async () => {
                    const due_at =
                      reviewAction === "callback"
                        ? zonedInstant(reviewTime, zone.zone)
                        : null;
                    try {
                      await localEdit(
                        api,
                        `/v1/leads/${packet.lead.id}/outreach`,
                        "PATCH",
                        {
                          request_id: crypto.randomUUID(),
                          expected_version: packet.lead.version,
                          expected_review_token: packet.review_token,
                          next_action: reviewAction,
                          reason: reviewReason,
                          due_at,
                          timezone: zone.zone,
                        },
                      );
                      notice("Next outreach action saved");
                    } finally {
                      onChange(
                        await api<Packet>(`/v1/leads/${packet.lead.id}/packet`),
                      );
                      await refresh();
                    }
                  })
                }
              >
                {reviewAction === "callback"
                  ? "Schedule reviewed callback"
                  : "Resume outreach after review"}
              </button>
            </section>
          )}
          {brief?.meeting && (
            <p>
              Meeting: {label(brief.meeting.status)} · rep-reported ·{" "}
              {date(brief.meeting.due_at, zone.zone)}
            </p>
          )}
          <details>
            <summary>Record a meeting outcome</summary>
            {brief?.meeting?.correction_reason && (
              <p role="status">{brief.meeting.correction_reason}</p>
            )}
            <p>
              You book the meeting in your own workflow. This records its
              outcome and outreach hold.
            </p>
            <label>
              Meeting status
              <select
                value={status}
                onChange={(e) => setStatus(e.target.value)}
              >
                {status === "needs_review" && (
                  <option value="needs_review" disabled>
                    Choose the corrected meeting status
                  </option>
                )}
                {["booked", "held", "no_show", "cancelled", "rescheduled"].map(
                  (x) => (
                    <option key={x} value={x}>
                      {label(x)}
                    </option>
                  ),
                )}
              </select>
            </label>
            <label>
              Reported meeting time ({zoneCaption(zone)})
              <input
                type="datetime-local"
                value={when}
                onChange={(e) => setWhen(e.target.value)}
              />
            </label>
            <label>
              Next outreach action
              <select value={next} onChange={(e) => setNext(e.target.value)}>
                <option value="">Choose next action</option>
                <option value="keep_hold">Keep outreach paused</option>
                <option value="resume">Resume when policy allows</option>
                <option value="callback">Confirmed callback</option>
              </select>
            </label>
            {next === "callback" && (
              <label>
                Callback time ({zoneCaption(zone)})
                <input
                  type="datetime-local"
                  value={callback}
                  onChange={(e) => setCallback(e.target.value)}
                />
              </label>
            )}
            <button
              className="small secondary"
              disabled={
                status === "needs_review" ||
                (next === "callback" && !callback) ||
                (["no_show", "cancelled"].includes(status) && !next)
              }
              onClick={() =>
                void run(async () => {
                  const due_at = when ? zonedInstant(when, zone.zone) : null;
                  const callback_at =
                    next === "callback"
                      ? zonedInstant(callback, zone.zone)
                      : null;
                  try {
                    await localEdit(
                      api,
                      `/v1/leads/${packet.lead.id}/meeting`,
                      "POST",
                      {
                        request_id: crypto.randomUUID(),
                        status,
                        expected_version: packet.lead.version,
                        expected_review_token: packet.review_token,
                        due_at,
                        timezone: zone.zone,
                        ...(next ? { next_action: next } : {}),
                        callback_at,
                      },
                    );
                    notice("Meeting recorded as rep-reported.");
                  } finally {
                    onChange(
                      await api<Packet>(`/v1/leads/${packet.lead.id}/packet`),
                    );
                    await refresh();
                  }
                })
              }
            >
              Save meeting outcome
            </button>
          </details>
        </details>
      </div>
    </div>
  );
}
