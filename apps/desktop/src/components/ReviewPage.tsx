import { ArrowLeft, Check, ChevronRight, MessageSquare } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useHistory } from "../state/useHistory";
import { HistoryMore } from "./HistoryMore";
import { date, label, useWorkspace } from "../context";
import type { CallReview } from "../types";
import { Empty } from "./Operations";
import { FollowupTasks } from "./FollowupTasks";
import { WrapUp } from "./WrapUp";
import { CallTimeline } from "./CallTimeline";
import { ReviewIssues } from "./ReviewIssues";
import { SummaryView } from "./SummaryView";

export function ReviewPage({
  onNavigate,
}: {
  onNavigate?: (page: string) => void;
}) {
  const { snapshot: w, api, run, notice, refresh } = useWorkspace();
  const [review, setReview] = useState<CallReview | null>(null);
  const [notes, setNotes] = useState("");
  const selectedCall = useRef<string | null>(null);
  const requestVersion = useRef(0);
  useEffect(
    () => () => {
      selectedCall.current = null;
      requestVersion.current++;
    },
    [],
  );
  async function reload(id: string, keepDraft = false) {
    if (selectedCall.current !== id) return;
    const version = ++requestVersion.current;
    const result = await api<CallReview>(`/v1/calls/${id}/review`);
    if (selectedCall.current !== id || requestVersion.current !== version)
      return;
    setReview(result);
    if (!keepDraft) setNotes(result.human_notes ?? "");
  }
  async function open(id: string) {
    selectedCall.current = id;
    requestVersion.current++;
    setNotes("");
    await reload(id);
  }
  const history = useHistory("calls", w.calls);
  const calls = history.items;
  if (!review)
    return (
      <>
        <div className="page-heading">
          <div>
            <h1>Review</h1>
            <p>What needs you, then your follow-ups and past calls.</p>
          </div>
          <label className="inline-label">
            Filter
            <select
              value={history.filter}
              onChange={(e) => {
                void run(() => history.load(false, "", e.target.value));
              }}
            >
              <option value="all">All calls</option>
              <option value="unconfirmed">Needs outcome review</option>
              <option value="wrong_party">Wrong party</option>
              <option value="callback_requested">Callbacks</option>
              <option value="meeting_held">Held meetings</option>
            </select>
          </label>
        </div>
        <ReviewIssues onNavigate={onNavigate} />
        <section
          className="card padded"
          aria-label="Follow-ups"
          data-review-section
        >
          <h2>Follow-ups</h2>
          <FollowupTasks />
        </section>
        <section className="card table-card">
          <table>
            <thead>
              <tr>
                <th>CONVERSATION</th>
                <th>STARTED</th>
                <th>OUTCOME</th>
                <th>STATE</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {calls.map((c) => (
                <tr key={c.id} data-call-id={c.id}>
                  <td>
                    <strong>
                      {c.lead_name ||
                        w.leads.find((l) => l.id === c.lead_id)?.name ||
                        "Lead"}
                    </strong>
                    <small>
                      {c.lead_company ||
                        w.leads.find((l) => l.id === c.lead_id)?.company}
                    </small>
                  </td>
                  <td>{date(c.started_at)}</td>
                  <td>
                    {c.outcome ? label(c.outcome) : "Awaiting confirmation"}
                  </td>
                  <td>
                    <span className="pill subtle">{label(c.state)}</span>
                  </td>
                  <td>
                    <button
                      className="text-button"
                      aria-label={`Review call ${c.lead_name || w.leads.find((l) => l.id === c.lead_id)?.name}`}
                      onClick={() => void run(() => open(c.id))}
                    >
                      Review call
                      <ChevronRight size={15} />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <HistoryMore history={history} label="Load older calls" />
          {!calls.length && !history.cursor && (
            <Empty text="Your completed conversations will appear here." />
          )}
        </section>
      </>
    );
  const summary = review.summary;
  return (
    <>
      <button
        className="text-button back"
        onClick={() => {
          selectedCall.current = null;
          setReview(null);
        }}
      >
        <ArrowLeft size={14} />
        All conversations
      </button>
      <div className="page-heading">
        <div>
          <span className="eyebrow">
            CALL REVIEW · {date(review.call.started_at)}
          </span>
          <h1>
            {w.leads.find((l) => l.id === review.call.lead_id)?.name ||
              "Conversation review"}
          </h1>
          <p>{label(review.call.outcome || "Outcome unconfirmed")}</p>
        </div>
      </div>
      <section className="card padded" aria-label="AI summary">
        <h2>Summary</h2>
        <SummaryView
          summary={summary}
          onRetry={() =>
            void run(async () => {
              await api(`/v1/calls/${review.call.id}/summary`, "POST");
              notice("Summary requested again.");
            })
          }
        />
      </section>
      <ConversionForm call={review.call} />
      {review.call.state === "FINALIZED" && (
        <details className="card padded">
          <summary>Correct confirmed outcome</summary>
          <p>
            Corrections update the lead and learning records. A changed meeting
            still needs an explicit next outreach decision.
          </p>
          <button
            className="secondary"
            onClick={() => void run(() => reload(review.call.id, true))}
          >
            Reload confirmed outcome
          </button>
          <WrapUp
            key={`${review.call.id}:${review.call.version}`}
            call={review.call}
            timezone={review.lead_timezone}
            onSaved={(result) =>
              void run(async () => {
                await reload(result.id);
                await refresh();
              })
            }
          />
        </details>
      )}
      <CallTimeline callId={review.call.id} />
      <div className="review-grid">
        <section className="card padded">
          <div className="card-eyebrow">
            <span>
              <MessageSquare size={15} /> TRANSCRIPT
            </span>
          </div>
          {review.transcript.length ? (
            review.transcript.map((s) => (
              <div className="review-segment" key={s.segment_id}>
                <div>
                  <strong>
                    {s.side === "rep"
                      ? "You"
                      : s.side === "remote"
                        ? "Prospect"
                        : "Unclear"}
                  </strong>
                  <small>
                    {s.start_ms === null
                      ? ""
                      : `${(s.start_ms / 1000).toFixed(1)}s`}
                  </small>
                </div>
                <p>{s.text}</p>
              </div>
            ))
          ) : (
            <Empty text="No transcript is saved for this call." />
          )}
          <p className="fine">
            Calls are not recorded. This transcript stays on this computer for
            30 days, then is deleted; notes and outcomes remain.
          </p>
          {review.transcript.length > 0 && (
            <button
              className="secondary small"
              onClick={() =>
                void run(async () => {
                  await api(`/v1/calls/${review.call.id}/transcript`, "DELETE");
                  await reload(review.call.id, true);
                  notice("Transcript deleted.");
                })
              }
            >
              Delete transcript
            </button>
          )}
          <div className="section-rule" />
          <ScriptTrace trace={review.script_trace} />
        </section>
        <section className="card padded">
          <h2>Your notes</h2>
          <label>
            Your notes
            <textarea
              aria-label="Your notes"
              rows={6}
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
            />
          </label>
          <button
            className="primary"
            onClick={() =>
              void run(async () => {
                await api(`/v1/calls/${review.call.id}/notes`, "PATCH", {
                  text: notes,
                });
                setReview({ ...review, human_notes: notes });
                notice("Notes saved");
              })
            }
          >
            Save notes
            <Check size={15} />
          </button>
        </section>
      </div>
    </>
  );
}

import { ConversionForm } from "./ConversionForm";

function seconds(ms: number | null | undefined) {
  return ms === null || ms === undefined ? "—" : `${Math.round(ms / 1000)}s`;
}

/** Which script sections were delivered, how the prospect reacted, and the call's shape (post-call). */
function ScriptTrace({ trace }: { trace?: CallReview["script_trace"] }) {
  if (!trace?.sections.length)
    return (
      <p className="muted small">
        No script section was recognized in this call.
      </p>
    );
  const shape = trace.shape;
  return (
    <div aria-label="Script sections delivered">
      <h3>Script sections delivered</h3>
      <ol>
        {trace.sections.map((s) => (
          <li key={s.key}>
            {label(s.key.split(":")[0])}
            {s.key.includes(":") ? ` ${Number(s.key.split(":")[1]) + 1}` : ""} ·
            prospect {s.reaction} · {seconds(s.portion_ms)}
          </li>
        ))}
      </ol>
      <p className="fine">
        {shape.timing === "measured"
          ? `Call shape (after the call only): rep talk ${shape.rep_talk_ratio === null ? "—" : `${Math.round(shape.rep_talk_ratio * 100)}%`}, longest monologue ${seconds(shape.longest_monologue_ms)}, first question at ${seconds(shape.time_to_first_question_ms)}, ${shape.discovery_questions_asked} discovery question(s).`
          : "Transcription timing was unavailable, so talk timing is unknown."}
      </p>
    </div>
  );
}
