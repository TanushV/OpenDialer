import { useRef, useState } from "react";
import { ApiError } from "../api/client";
import { date, label, useWorkspace } from "../context";
import type { Analysis } from "../types";

export function ScoreReview({
  analysis,
  evidence,
  reload,
}: {
  analysis: Analysis;
  evidence: string[];
  reload: () => Promise<void>;
}) {
  const { api, run, notice } = useWorkspace();
  const [status, setStatus] = useState("disputed");
  const [dimension, setDimension] = useState("");
  const [score, setScore] = useState(2);
  const [reason, setReason] = useState("");
  const [resolves, setResolves] = useState("");
  const [busy, setBusy] = useState(false);
  const pending = useRef<{
    signature: string;
    body: Record<string, unknown>;
  } | null>(null);
  const rating = ["SCORED", "N/A", "INSUFFICIENT_EVIDENCE"].includes(status);
  const selected = analysis.scorecard.items.find(
    (item) => item.dimension === dimension,
  );
  const sources = evidence.length ? evidence : selected?.evidence || [];
  const humanItems =
    analysis.reviewed_scorecard?.items.filter(
      (item) => item.source === "human",
    ) || [];
  return (
    <section aria-label="Human score review">
      <h3>Human score review</h3>
      <p className="small muted">
        AI assessment remains available above. Human ratings identify their
        author and evidence. A source or purpose correction requires another
        review.
      </p>
      {humanItems.map((item) => (
        <div className="score-row" key={item.dimension}>
          <div>
            <strong>
              Human rating · {item.title || label(item.dimension)}
            </strong>
            <small>{item.explanation}</small>
          </div>
          <span>
            {item.score === null ? label(item.status) : `${item.score}/2`}
          </span>
        </div>
      ))}
      <form
        onSubmit={(event) => {
          event.preventDefault();
          setBusy(true);
          const values = {
            reason,
            status,
            dimension: dimension || null,
            score: status === "SCORED" ? score : null,
            evidence: sources,
            resolves_review_id: resolves || null,
          };
          const signature = JSON.stringify(values);
          if (pending.current?.signature !== signature)
            pending.current = {
              signature,
              body: {
                ...values,
                expected_version: analysis.scorecard_version,
                request_id: crypto.randomUUID(),
              },
            };
          const body = pending.current.body;
          void run(async () => {
            try {
              await api(
                `/v1/scorecards/${analysis.scorecard_id}/review`,
                "POST",
                body,
              );
              pending.current = null;
              setReason("");
              setResolves("");
              notice(
                "Review saved with its original assessment and source versions.",
              );
            } catch (error) {
              if (
                error instanceof ApiError &&
                error.status >= 400 &&
                error.status < 500
              )
                pending.current = null;
              throw error;
            } finally {
              try {
                await reload();
              } finally {
                setBusy(false);
              }
            }
          });
        }}
      >
        <label>
          Review action
          <select
            aria-label="Review action"
            value={status}
            onChange={(event) => setStatus(event.target.value)}
          >
            <option value="disputed">Challenge assessment</option>
            <option value="SCORED">Record human rating</option>
            <option value="N/A">No opportunity (N/A)</option>
            <option value="INSUFFICIENT_EVIDENCE">Insufficient evidence</option>
            <option value="resolved">
              Resolve an appeal without changing ratings
            </option>
          </select>
        </label>
        <label>
          Review dimension
          <select
            aria-label="Review dimension"
            value={dimension}
            required={rating}
            onChange={(event) => setDimension(event.target.value)}
          >
            <option value="">Whole assessment</option>
            {analysis.scorecard.items.map((item) => (
              <option key={item.dimension} value={item.dimension}>
                {item.title || label(item.dimension)}
              </option>
            ))}
          </select>
        </label>
        {status === "SCORED" && (
          <>
            <label>
              Human score
              <select
                aria-label="Human score"
                value={score}
                onChange={(event) => setScore(Number(event.target.value))}
              >
                <option value={0}>
                  0 · Not demonstrated despite opportunity
                </option>
                <option value={1}>1 · Partial</option>
                <option value={2}>2 · Clearly demonstrated</option>
              </select>
            </label>
            <p className="small">
              {sources.length
                ? `${sources.length} supporting moment(s) selected.`
                : "Select supporting transcript moments or a dimension with retained evidence before recording a numerical rating."}
            </p>
          </>
        )}
        <label>
          Resolve existing appeal
          <select
            aria-label="Resolve existing appeal"
            value={resolves}
            required={status === "resolved"}
            disabled={status === "disputed"}
            onChange={(event) => setResolves(event.target.value)}
          >
            <option value="">No appeal selected</option>
            {analysis.reviews
              .filter(
                (item) => item.status === "disputed" && !item.resolution_id,
              )
              .map((item) => (
                <option key={item.id} value={item.id}>
                  {item.reason}
                </option>
              ))}
          </select>
        </label>
        <label>
          Review reason
          <textarea
            aria-label="Review reason"
            rows={2}
            maxLength={1000}
            required
            value={reason}
            onChange={(event) => setReason(event.target.value)}
          />
        </label>
        <button
          className="secondary"
          disabled={
            busy || !reason.trim() || (status === "SCORED" && !sources.length)
          }
        >
          Save review
        </button>
      </form>
      {analysis.reviews.map((item, index) => (
        <p className="small" key={item.id || index}>
          {item.reason} · {label(item.status)} ·{" "}
          {item.reviewer_role || "Reviewer"}
          {item.at ? ` · ${date(item.at)}` : ""}
          {item.resolution_id ? " · Resolved" : ""}
          {item.applies === false ? " · Earlier context; requires review" : ""}
          {item.raw_evidence_status === "expired"
            ? " · Original speech expired"
            : ""}
        </p>
      ))}
    </section>
  );
}
