import { useState } from "react";
import { date, label, useWorkspace } from "../context";
import type { Packet } from "../types";
import { localDateInput } from "./FollowupTasks";

export function ProspectBrief({
  packet,
  onChange,
}: {
  packet: Packet;
  onChange: (packet: Packet) => void;
}) {
  const { api, run, refresh, notice } = useWorkspace();
  const brief = packet.prospect_brief;
  const stats = packet.statistical_guidance;
  const [status, setStatus] = useState(brief?.meeting?.status || "booked");
  const [next, setNext] = useState("keep_hold");
  const [when, setWhen] = useState(localDateInput(brief?.meeting?.due_at));
  const [callback, setCallback] = useState("");
  const [reviewAction, setReviewAction] = useState("resume");
  const [reviewReason, setReviewReason] = useState("");
  const [reviewTime, setReviewTime] = useState("");
  return (
    <div className="prospect-context">
      <div data-testid="statistical-guidance" className="small">
        <strong>Calling context</strong>
        {stats && (
          <>
            <p>
              {Math.round(stats.human_probability * 100)}% estimated pickup ·{" "}
              {stats.samples} observations
            </p>
            <p className="muted">{stats.why_now}</p>
            <details>
              <summary>Estimates &amp; uncertainty</summary>
              <p>{stats.uncertainty}</p>
              <p>
                Right party after human pickup:{" "}
                {Math.round(stats.right_party_given_human * 100)}%.{" "}
                {label(stats.conversion_goal)} after right-party contact:{" "}
                {Math.round(stats.conversion_given_right_party * 100)}% (
                {stats.conversion_samples} matured observations).
              </p>
              <p>
                {stats.as_of
                  ? `Updated ${date(stats.as_of)}`
                  : "Pooled starting assumptions; no measured outcome estimate yet."}
              </p>
            </details>
          </>
        )}
      </div>
      <details data-testid="prospect-brief" className="small">
        <summary>Prior conversation &amp; next step</summary>
        <p>{brief?.summary || "No previous interpreted conversation."}</p>
        {brief?.last_outcome && (
          <p>Last outcome: {label(brief.last_outcome)}</p>
        )}
        {brief?.human_notes.map((n) => (
          <p key={n.call_id}>Rep note: {n.text}</p>
        ))}
        {brief?.needs.map((n, i) => (
          <p key={`need-${i}`}>{n.text}</p>
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
        {packet.commitments.map((c, i) => (
          <p key={i}>
            {c.action} · {date(c.due_at)}
          </p>
        ))}
        <p>Next question: {brief?.next_question}</p>
        {packet.lead.outreach_hold && (
          <p role="status">
            Outreach paused: {label(packet.lead.outreach_hold)}
          </p>
        )}
        {packet.lead.outreach_hold === "goal_changed" && (
          <section aria-label="Review next outreach">
            <p>
              The campaign goal changed. Review the relationship and choose the
              next action.
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
                Reviewed callback time (your device timezone)
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
                  try {
                    await api(`/v1/leads/${packet.lead.id}/outreach`, "PATCH", {
                      expected_version: packet.lead.version,
                      expected_review_token: packet.review_token,
                      next_action: reviewAction,
                      reason: reviewReason,
                      due_at:
                        reviewAction === "callback"
                          ? new Date(reviewTime).toISOString()
                          : null,
                      timezone:
                        Intl.DateTimeFormat().resolvedOptions().timeZone,
                    });
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
            {date(brief.meeting.due_at)}
          </p>
        )}
        <details>
          <summary>Record a meeting outcome</summary>
          <p>
            You book the meeting in your own workflow. This records its outcome
            and outreach hold.
          </p>
          <label>
            Meeting status
            <select value={status} onChange={(e) => setStatus(e.target.value)}>
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
            Reported meeting time (your device timezone)
            <input
              type="datetime-local"
              value={when}
              onChange={(e) => setWhen(e.target.value)}
            />
          </label>
          <label>
            Next outreach action
            <select value={next} onChange={(e) => setNext(e.target.value)}>
              <option value="keep_hold">Keep outreach paused</option>
              <option value="resume">Resume when policy allows</option>
              <option value="callback">Confirmed callback</option>
            </select>
          </label>
          {next === "callback" && (
            <label>
              Callback time (your device timezone)
              <input
                type="datetime-local"
                value={callback}
                onChange={(e) => setCallback(e.target.value)}
              />
            </label>
          )}
          <button
            className="small secondary"
            disabled={next === "callback" && !callback}
            onClick={() =>
              void run(async () => {
                try {
                  await api(`/v1/leads/${packet.lead.id}/meeting`, "POST", {
                    status,
                    expected_version: packet.lead.version,
                    expected_review_token: packet.review_token,
                    due_at: when ? new Date(when).toISOString() : null,
                    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
                    next_action: next,
                    callback_at:
                      next === "callback"
                        ? new Date(callback).toISOString()
                        : null,
                  });
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
  );
}
