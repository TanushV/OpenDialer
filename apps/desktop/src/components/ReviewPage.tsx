import { useSpeechExpiry } from "../state/useSpeechExpiry";
import {
  ArrowLeft,
  Check,
  ChevronRight,
  MessageSquare,
  Quote,
  ShieldCheck,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useHistory } from "../state/useHistory";
import { HistoryMore } from "./HistoryMore";
import { date, label, useWorkspace } from "../context";
import type { Analysis } from "../types";
import { Empty } from "./Operations";
import { ConversationObservations } from "./ConversationObservations";
import { CallPurpose } from "./Rubrics";
import { ScoreReview } from "./ScoreReview";
import { Coaching, ReviewTip } from "./Coaching";
import { FollowupTasks } from "./FollowupTasks";
import { WrapUp } from "./WrapUp";

export function ReviewPage() {
  const { snapshot: w, api, run, notice, refresh } = useWorkspace();
  const [analysis, setAnalysis] = useState<Analysis | null>(null);
  useSpeechExpiry(analysis, setAnalysis);
  // Per-call drafts: the human note, a comment and selected evidence.
  const [notes, setNotes] = useState("");
  const [comment, setComment] = useState("");
  const [highlight, setHighlight] = useState<string[]>([]);
  const savedNotes = useRef<string | null>(null);
  const selectedCall = useRef<string | null>(null);
  const requestVersion = useRef(0);
  useEffect(
    () => () => {
      selectedCall.current = null;
      requestVersion.current++;
    },
    [],
  );
  async function reload(id: string) {
    if (selectedCall.current !== id) return;
    const version = ++requestVersion.current;
    const result = await api<Analysis>(`/v1/calls/${id}/analysis`);
    if (selectedCall.current !== id || requestVersion.current !== version)
      return;
    setAnalysis(result);
    // Only a human writes "Your notes"; the AI summary is shown separately.
    // An unchanged draft follows the saved note; an edited draft is kept.
    const previous = savedNotes.current;
    savedNotes.current = result.human_notes;
    setNotes((draft) =>
      draft === (previous ?? "") ? (result.human_notes ?? "") : draft,
    );
  }
  function select(id: string | null) {
    if (selectedCall.current === id) return;
    selectedCall.current = id;
    requestVersion.current++;
    savedNotes.current = null;
    setNotes("");
    setComment("");
    setHighlight([]);
  }
  async function open(id: string) {
    select(id);
    await reload(id);
  }
  const history = useHistory("calls", w.calls);
  const calls = history.items;
  if (!analysis)
    return (
      <>
        <div className="page-heading">
          <div>
            <h1>Review</h1>
            <p>Coaching, follow-ups and your past calls.</p>
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
        <Coaching onReview={open} />
        <section className="card padded" aria-label="Follow-ups">
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
  const evidence = (ids: string[]) => (
    <button className="evidence-button" onClick={() => setHighlight(ids)}>
      <Quote size={12} />
      {ids.join(", ")}
    </button>
  );
  return (
    <>
      <button
        className="text-button back"
        onClick={() => {
          select(null);
          setAnalysis(null);
        }}
      >
        <ArrowLeft size={14} />
        All conversations
      </button>
      <div className="page-heading">
        <div>
          <span className="eyebrow">
            CALL REVIEW · {date(analysis.call.started_at)}
          </span>
          <h1>
            {w.leads.find((l) => l.id === analysis.call.lead_id)?.name ||
              "Conversation review"}
          </h1>
          <p>
            {label(analysis.call.outcome || "Outcome unconfirmed")} · Transcript
            version {analysis.notes.transcript_version} ·{" "}
            {label(analysis.status)}
          </p>
        </div>
        <span className="pill subtle">
          {analysis.provider === "extractive_baseline"
            ? "Extractive baseline · review required"
            : "AI draft · review required"}
        </span>
      </div>
      {analysis.call.analysis_incomplete && (
        <p
          className="notice"
          role="status"
          aria-label="Incomplete call analysis"
        >
          {analysis.call.unprocessed_speech_events ?? "Some"} speech events
          could not be processed before their raw content expired or became
          unavailable. The AI rundown may be incomplete. Review your notes and
          follow-up.
        </p>
      )}
      {analysis.grounding_status === "review_required" && (
        <p className="notice" role="status" aria-label="Evidence review">
          AI interpretation needs review. Its evidence check was unavailable or
          incomplete. Review your notes and follow-up before relying on the AI
          rundown.
        </p>
      )}
      <ConversionForm call={analysis.call} />
      {analysis.call.state === "FINALIZED" && (
        <details className="card padded">
          <summary>Correct confirmed outcome</summary>
          <p>
            Corrections update the lead and learning records. A changed meeting
            still needs an explicit next outreach decision.
          </p>
          <button
            className="secondary"
            onClick={() => void run(() => reload(analysis.call.id))}
          >
            Reload confirmed outcome
          </button>
          <WrapUp
            key={`${analysis.call.id}:${analysis.call.version}`}
            call={analysis.call}
            timezone={analysis.lead_timezone}
            onSaved={(result) =>
              void run(async () => {
                await reload(result.id);
                await refresh();
              })
            }
          />
        </details>
      )}
      <section className="card padded" aria-label="Retained call context">
        <h2>Retained call context</h2>
        <p className="small muted">
          Brief interpretations from the live helper. These are provisional
          context, separate from confirmed outcomes and the reviewed scorecard.
          Original speech expires.
        </p>
        {analysis.interpretation_coverage && (
          <p className="small">
            {analysis.interpretation_coverage.interpreted_segments} of{" "}
            {analysis.interpretation_coverage.total_segments} stable chunks
            interpreted.
            {analysis.interpretation_coverage.pending_segments > 0
              ? ` ${analysis.interpretation_coverage.pending_segments} awaiting interpretation.`
              : ""}
            {analysis.interpretation_coverage.unavailable_segments > 0
              ? ` ${analysis.interpretation_coverage.unavailable_segments} could not be interpreted before their raw content became unavailable.`
              : ""}
          </p>
        )}
        {(
          [
            ["rundown_points", "Rundown"],
            ["open_questions", "Open questions"],
            ["coaching_points", "Coaching tips"],
          ] as const
        ).map(([key, title]) => {
          const items = analysis.call_memory?.[key] || [];
          return items.length ? (
            <div key={key}>
              <h3>{title}</h3>
              <ul>
                {items.map((item, index) => (
                  <li key={item.id || index}>
                    {item.text}
                    {item.status === "resolved" ? " · Resolved" : ""}
                    {key === "coaching_points" && item.id && (
                      <ReviewTip
                        callId={analysis.call.id}
                        version={analysis.call.transcript_version}
                        pointId={item.id}
                      />
                    )}
                  </li>
                ))}
              </ul>
            </div>
          ) : null;
        })}
        {!!analysis.call_memory?.memory_omitted_items && (
          <p className="small muted">
            {analysis.call_memory.memory_omitted_items} older context items were
            omitted from the compact brief. This is not a complete transcript or
            account of every detail.
          </p>
        )}
      </section>
      <div className="review-grid">
        <section className="card padded">
          <div className="card-eyebrow">
            <span>
              <MessageSquare size={15} /> TRANSCRIPT &amp; EVIDENCE
            </span>
          </div>
          {analysis.transcript.length ? (
            analysis.transcript.map((s) => (
              <div
                className={`review-segment ${highlight.includes(s.segment_id) ? "highlight" : ""}`}
                key={s.segment_id}
              >
                <div>
                  <strong>
                    {s.side === "rep"
                      ? "Rep side"
                      : s.side === "remote"
                        ? "Remote side"
                        : "Uncertain side"}
                  </strong>
                  <small>
                    {s.start_ms === null
                      ? "Timing unavailable"
                      : `${(s.start_ms / 1000).toFixed(1)}s`}{" "}
                    · {s.is_final ? "Final" : "Interim"} · r{s.revision}
                  </small>
                </div>
                <p>{s.text}</p>
              </div>
            ))
          ) : (
            <Empty text="No permitted final transcript is available for this call." />
          )}
          <p className="fine">
            Call recordings are disabled. Raw evidence expires within five
            minutes; retained notes and outcomes remain.
          </p>
          <div className="section-rule" />
          <h3>Observable conversation metrics</h3>
          <p className="muted small">
            {String(analysis.metrics.timed_segments)} timed segments /{" "}
            {String(analysis.metrics.total_segments)} total.{" "}
            {analysis.metrics.rep_talk_ratio === null
              ? "Talk ratio unavailable."
              : `Rep-side talk ratio ${Math.round(Number(analysis.metrics.rep_talk_ratio) * 100)}%.`}{" "}
            {String(analysis.metrics.question_count)} rep questions.
          </p>
          <ScriptTrace trace={analysis.script_trace} />
        </section>
        <div>
          <section className="card padded">
            <span className="eyebrow">THE RECORD YOU CONTROL</span>
            <h2>Notes &amp; next steps</h2>
            <div className="info-note" role="region" aria-label="AI summary">
              <strong>AI summary · draft, not your note</strong>
              <p>
                {analysis.notes.summary || "No AI summary is available yet."}
              </p>
            </div>
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
                  const latest = await api<Analysis>(
                    `/v1/calls/${analysis.call.id}/analysis`,
                  );
                  if (latest.human_notes !== analysis.human_notes)
                    throw new Error(
                      "Another human changed these notes. Your text is retained; review the saved version before replacing it.",
                    );
                  const result = await api<{
                    notes_version: number;
                    notes_edit_version: number;
                  }>(`/v1/calls/${analysis.call.id}/notes`, "PATCH", {
                    expected_edit_version: latest.notes_edit_version ?? 0,
                    text: notes,
                  });
                  savedNotes.current = notes;
                  setAnalysis({
                    ...latest,
                    notes_version: result.notes_version,
                    notes_edit_version: result.notes_edit_version,
                    human_notes: notes,
                  });
                  notice("Notes saved");
                })
              }
            >
              Save notes
              <Check size={15} />
            </button>
            {analysis.suggested_update !== null && (
              <p className="fine" role="status">
                The AI summary above changed after your note was saved. Your
                note is preserved.
              </p>
            )}
            <h3>Objections</h3>
            {analysis.notes.objections.length ? (
              analysis.notes.objections.map((o, i) => (
                <p key={i}>
                  {o.text} {evidence(o.evidence)}
                </p>
              ))
            ) : (
              <p className="muted small">No evidenced objection extracted.</p>
            )}
            <h3>Commitments</h3>
            {analysis.notes.commitments.length ? (
              analysis.notes.commitments.map((c, i) => (
                <p key={i}>
                  {c.action}
                  <small>
                    {c.needs_time_confirmation
                      ? "Time needs confirmation"
                      : date(c.due_at)}
                  </small>
                  {evidence(c.evidence)}
                </p>
              ))
            ) : (
              <p className="muted small">No confirmed commitment extracted.</p>
            )}
          </section>
          <ConversationObservations
            analysis={analysis}
            reload={() => reload(analysis.call.id)}
          />
          <section className="card padded" aria-label="Conversation signals">
            <h2>Conversation signals</h2>
            <p className="small muted">
              Configured concepts with supporting moments. A match describes the
              conversation; it is not a conversion outcome or personality
              judgment.
            </p>
            {!analysis.trackers?.length && (
              <p className="small">No supported signals are available.</p>
            )}
            {analysis.trackers?.map((item, index) => (
              <div key={`${item.tracker_id}:${index}`}>
                <strong>{item.name || "Configured signal"}</strong>
                <p className="small">
                  {label(item.kind || "semantic")} · Definition{" "}
                  {item.version ?? "unknown"}
                  {item.raw_evidence_status === "expired"
                    ? " · Original speech expired"
                    : ""}
                </p>
                {item.raw_evidence_status !== "expired" &&
                  evidence(item.evidence)}
              </div>
            ))}
            {!!analysis.tracker_definitions_omitted && (
              <p className="notice">
                {analysis.tracker_definitions_omitted} definitions exceeded the
                20-signal analysis limit. Narrow the active definitions before
                relying on their absence.
              </p>
            )}
          </section>
          <section className="card padded">
            <div className="card-eyebrow">
              <span>
                <ShieldCheck size={15} /> REVIEWABLE SCORECARD
              </span>
              <span>
                {analysis.scorecard.overall.score === null
                  ? "Insufficient evidence"
                  : `${Math.round(analysis.scorecard.overall.score)}/100`}
              </span>
            </div>
            <CallPurpose
              analysis={analysis}
              reload={() => reload(analysis.call.id)}
            />
            <p className="muted small">
              Evidence coverage:{" "}
              {Math.round(analysis.scorecard.overall.coverage * 100)}%. N/A and
              insufficient-evidence dimensions do not lower an overall score.
            </p>
            {analysis.scorecard.items.map((item) => (
              <div className="score-row" key={item.dimension}>
                <div>
                  <strong>{item.title || label(item.dimension)}</strong>
                  <small>{item.explanation}</small>
                </div>
                <span className="pill subtle">
                  {item.score === null ? label(item.status) : `${item.score}/2`}
                </span>
              </div>
            ))}
            <ScoreReview
              key={analysis.call.id}
              analysis={analysis}
              evidence={highlight}
              reload={() => reload(analysis.call.id)}
            />
          </section>
          <section className="card padded">
            <h3>Review comments</h3>
            {analysis.comments.map((c, i) => (
              <p key={i}>{c.text}</p>
            ))}
            <label>
              Comment
              <input
                value={comment}
                onChange={(e) => setComment(e.target.value)}
              />
            </label>
            <button
              className="secondary"
              disabled={!comment}
              onClick={() =>
                void run(async () => {
                  await api(`/v1/calls/${analysis.call.id}/comments`, "POST", {
                    text: comment,
                  });
                  await reload(analysis.call.id);
                  setComment("");
                })
              }
            >
              Add comment
            </button>
          </section>
        </div>
      </div>
    </>
  );
}
import { ConversionForm } from "./ConversionForm";

function seconds(ms: number | null | undefined) {
  return ms === null || ms === undefined ? "—" : `${Math.round(ms / 1000)}s`;
}

/** Which script sections were delivered, how the prospect reacted, and the call's shape (post-call). */
function ScriptTrace({ trace }: { trace?: Analysis["script_trace"] }) {
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
