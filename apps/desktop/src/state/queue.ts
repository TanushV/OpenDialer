import { ApiError } from "../api/client";

/** The authority can look further: its scan passed a page of leads that cannot be
 * called now, or another rep's concurrent claim took the chosen lead or number. */
const continues = (error: unknown) =>
  error instanceof ApiError &&
  (error.code === "QUEUE_SCAN_CONTINUES" || error.code === "QUEUE_CHANGED");

/**
 * Next lead, retrying a bounded number of times while the authority's queue
 * scan continues or a concurrent claim changed the queue.
 */
export async function nextLeadWithRetry<T>(
  attempt: () => Promise<T>,
  {
    tries = 5,
    delayMs = 400,
    onWaiting,
  }: {
    tries?: number;
    delayMs?: number;
    onWaiting: (waiting: boolean) => void;
  },
): Promise<T> {
  try {
    for (let tried = 1; ; tried++) {
      try {
        return await attempt();
      } catch (error) {
        if (!continues(error)) throw error;
        if (tried >= tries)
          throw new Error(
            "Still finding an eligible lead. Many leads cannot be called right now; try again in a moment.",
          );
        onWaiting(true);
        await new Promise((resolve) => setTimeout(resolve, delayMs));
      }
    }
  } finally {
    onWaiting(false);
  }
}
