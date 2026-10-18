type WorkCall = {
  rep_id: string;
  state: string;
  ended_at?: string;
  detached?: boolean;
};
const ended = new Set([
  "ENDED",
  "FINALIZED",
  "BUSY",
  "NO_ANSWER",
  "TEMPORARY_FAILURE",
  "INVALID_ENDPOINT",
]);
export function heartbeatActivity(
  state: { presence: string; repId: string; calls: WorkCall[] },
  idleMs: number,
  now: number,
): "ready" | "call" | "wrapup" | "idle" | null {
  const owned = state.calls.filter(
    (call) => call.rep_id === state.repId && !call.detached,
  );
  if (owned.some((call) => !ended.has(call.state))) return "call";
  if (
    idleMs < 15 * 60_000 &&
    owned.some(
      (call) =>
        call.state !== "FINALIZED" &&
        call.ended_at &&
        now - Date.parse(call.ended_at) >= 0 &&
        now - Date.parse(call.ended_at) < 15 * 60_000,
    )
  )
    return "wrapup";
  if (!["ready", "busy"].includes(state.presence)) return null;
  return idleMs >= 15 * 60_000 ? "idle" : "ready";
}
