import { useState } from "react";

/**
 * Requests that may overlap (a re-read and a save, or two re-reads) answer in any order. Only an answer no later
 * request has already beaten may show, so a slow older answer never undoes a newer one, success or error alike.
 */
export function newestGate() {
  let started = 0;
  let shown = 0;
  return {
    begin: () => ++started,
    /** True if this answer may show; it then becomes the newest shown. */
    accept: (ticket: number) => {
      if (ticket < shown) return false;
      shown = ticket;
      return true;
    },
  };
}
export type NewestGate = ReturnType<typeof newestGate>;

/** One read through the gate: its value shows only if no later read has shown first. A failed read still throws. */
export async function readNewest<T>(
  gate: NewestGate,
  load: () => Promise<T>,
  show: (value: T) => void,
) {
  const ticket = gate.begin();
  const value = await load();
  if (gate.accept(ticket)) show(value);
}

export function useNewestAnswer() {
  const [gate] = useState(newestGate);
  return gate;
}
