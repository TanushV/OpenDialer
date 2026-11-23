import type { Call } from "../types";

/** A summary saved before 2.4.2; none is written now that transcripts are not kept. */
export function SummaryView({
  summary,
}: {
  summary: NonNullable<Call["aftercall"]>;
}) {
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
      {summary.error || "The summary could not be written."}
    </p>
  );
}
