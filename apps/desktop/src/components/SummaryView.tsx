import type { Call } from "../types";

/** The after-call summary, or why there is none and what to do. */
export function SummaryView({
  summary,
  onRetry,
}: {
  summary: Call["aftercall"];
  onRetry?: () => void;
}) {
  if (!summary) return <p className="muted small">Writing the summary…</p>;
  if (summary.status === "ready")
    return (
      <>
        <p>{summary.summary}</p>
        {summary.next_step && (
          <p className="muted small">Next: {summary.next_step}</p>
        )}
      </>
    );
  if (summary.status === "no_speech")
    return <p className="muted small">No one spoke on this call.</p>;
  return (
    <p className="muted small">
      {summary.error || "The summary could not be written."}{" "}
      {onRetry && (
        <button className="text-button" onClick={onRetry}>
          Retry
        </button>
      )}
    </p>
  );
}
