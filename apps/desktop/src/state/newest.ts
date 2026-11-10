import { useMemo, useRef } from "react";

/**
 * Requests that may overlap (a re-read and a save, or two re-reads) answer in any order. Only an answer no later
 * request has already beaten may show, so a slow older answer never undoes a newer one, success or error alike.
 */
export function useNewestAnswer() {
  const started = useRef(0);
  const shown = useRef(0);
  return useMemo(
    () => ({
      begin: () => ++started.current,
      /** True if this answer may show; it then becomes the newest shown. */
      accept: (ticket: number) => {
        if (ticket < shown.current) return false;
        shown.current = ticket;
        return true;
      },
    }),
    [],
  );
}
