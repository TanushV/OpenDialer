import { isTerminal } from "../types";

type Ended = {
  state: string;
  provider_hangup_source?: string | null;
  connected_seconds?: number | null;
};

/** "They hung up · 0:23" or "You ended the call · 0:23" once a call has ended; null while it is live. */
export function endedSummary(call: Ended | null | undefined): string | null {
  if (!call || !isTerminal(call)) return null;
  const who =
    call.provider_hangup_source === "callee"
      ? "They hung up"
      : call.provider_hangup_source === "caller"
        ? "You ended the call"
        : "Call ended";
  if (call.connected_seconds == null) return who;
  const total = Math.round(call.connected_seconds);
  const seconds = String(total % 60).padStart(2, "0");
  return `${who} · ${Math.floor(total / 60)}:${seconds}`;
}
