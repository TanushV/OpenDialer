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
      <div data-testid="calling-context" className="small">
        <strong>Calling context</strong>
        {brief?.why_queued && (
          <p className="muted">Why this lead: {brief.why_queued}</p>
        )}
      </div>
      <WhyNow lead={packet.lead} />
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
        {brief?.next_step && <p>Agreed next step: {brief.next_step}</p>}
        {brief?.next_question && <p>Next question: {brief.next_question}</p>}
        <details>
          <summary>Earlier calls</summary>
          {packet.context_coverage &&
            packet.context_coverage.calls_omitted > 0 && (
              <p className="muted">
                {packet.context_coverage.calls_omitted} older calls are not
                shown.
              </p>
            )}
          {brief?.earlier_calls?.length ? (
            brief.earlier_calls.map((c) => (
              <p key={c.call_id}>
                {date(c.at, zone.zone)} ·{" "}
                {c.outcome ? label(c.outcome) : "no outcome"}
                {c.summary ? `: ${c.summary}` : ""}
                {c.next_step ? ` Next: ${c.next_step}` : ""}
              </p>
            ))
          ) : (
            <p>{brief?.summary || "No earlier call summary yet."}</p>
          )}
          {brief?.freshness && (
            <p>Preparation: {label(brief.freshness.state)}</p>
          )}
          {brief?.human_notes.map((n) => (
            <p key={n.call_id}>Your note: {n.text}</p>
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

/** The checked trigger, its evidence and the bridge line, plus the number's status (section 9, R9.8, R9.22). */
export function WhyNow({ lead }: { lead: Packet["lead"] }) {
  const trigger = lead.trigger;
  const found = trigger && trigger.type !== "none";
  return (
    <div data-testid="why-now" className="small why-now">
      <p>
        <strong>Why now:</strong>{" "}
        {found
          ? `${label(trigger.type)} · ${lead.trigger_label}`
          : "No trigger found"}
        {found && trigger.date ? ` · ${trigger.date}` : ""}
      </p>
      {found && trigger.quote && <p className="muted">“{trigger.quote}”</p>}
      {found && trigger.source_url && (
        <p>
          <a href={trigger.source_url} target="_blank" rel="noreferrer">
            {trigger.evidence === "row_only" ? "Link from the list" : "Source"}
          </a>
        </p>
      )}
      {lead.bridge?.text && (
        <p>
          Bridge: {lead.bridge.text} <span className="muted">(inference)</span>
        </p>
      )}
      {lead.number_label && (
        <p className={lead.number_hold ? "number-held" : undefined}>
          Number: {lead.number_label}
          {lead.number_hold_reason ? ` · ${lead.number_hold_reason}` : ""}
        </p>
      )}
    </div>
  );
}
