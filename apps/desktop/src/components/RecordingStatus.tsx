import { Button } from "../ui/kit";
import type { Call } from "../types";

/** Plain words for why the prospect's side is not kept on this call. */
export function recordingReason(reason: string) {
  if (reason === "toll_free") return "toll-free number";
  if (reason === "unknown") return "state unknown";
  if (reason === "outside_us") return "outside the US";
  return reason;
}

/**
 * What this call keeps, always visible while talking (call insights plan C4), and the R key that keeps the
 * prospect's side once the rep has told them the call is recorded.
 */
export function RecordingStatus({
  call,
  onConsent,
}: {
  call: Call;
  onConsent: () => void;
}) {
  const rules = call.recording;
  if (!rules) return null;
  const both = rules.prospect || !!rules.consent_at;
  const reason = recordingReason(rules.reason);
  const lines = [
    both
      ? rules.rep
        ? "Recording both sides"
        : "Recording the prospect's side"
      : rules.rep
        ? "Recording your side"
        : "Not recording",
  ];
  if (!both) lines.push(`Prospect audio not kept: ${reason}`);
  if (!both && !rules.prospect_live)
    lines.push(`Prospect not transcribed: ${reason}`);
  return (
    <div
      className="recording-status"
      role="status"
      aria-label="Recording"
      data-testid="recording-status"
    >
      <span className="fine">{lines.join(" · ")}</span>
      {!both && (
        <Button variant="secondary" kbd="R" onClick={onConsent}>
          They know it's recorded
        </Button>
      )}
    </div>
  );
}
