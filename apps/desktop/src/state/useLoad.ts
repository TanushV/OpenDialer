import { useCallback, useEffect, useState } from "react";
import { useWorkspace } from "../context";
import { useNewestAnswer } from "./newest";

/**
 * One authority read, refetched when its path changes. Re-reads overlap (an edit's own re-read and the event it
 * caused), so only the newest answer shows; a late result for an old path is dropped.
 */
export function useLoad<T>(path: string | null) {
  const { api } = useWorkspace();
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");
  const newest = useNewestAnswer();
  useEffect(() => {
    if (!path) return;
    let current = true;
    const ticket = newest.begin();
    api<T>(path)
      .then((value) => {
        if (current && newest.accept(ticket)) setData(value);
      })
      .catch((reason: unknown) => {
        if (current && newest.accept(ticket))
          setError(reason instanceof Error ? reason.message : String(reason));
      });
    return () => {
      current = false;
    };
  }, [api, path, newest]);
  const reload = useCallback(async () => {
    if (!path) return;
    const ticket = newest.begin();
    const value = await api<T>(path);
    if (newest.accept(ticket)) setData(value);
  }, [api, path, newest]);
  return { data, setData, error, setError, reload };
}
