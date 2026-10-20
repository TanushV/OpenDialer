import { ApiError } from "../api/client";

/** The authority examined a full page of currently denied leads and can look further. */
const continues = (error: unknown) =>
  error instanceof ApiError && error.code === "QUEUE_SCAN_CONTINUES";

/**
 * Next lead, retrying a bounded number of times while the authority's queue
 * scan continues past leads that cannot be called now.
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
