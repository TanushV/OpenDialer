import { useCallback, useEffect, useState } from "react";
import { useWorkspace } from "../context";

/** One authority read, refetched when its path changes; a late result for an old path is dropped. */
export function useLoad<T>(path: string | null) {
  const { api } = useWorkspace();
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!path) return;
    let current = true;
    api<T>(path)
      .then((value) => {
        if (current) setData(value);
      })
      .catch((reason: unknown) => {
        if (current)
          setError(reason instanceof Error ? reason.message : String(reason));
      });
    return () => {
      current = false;
    };
  }, [api, path]);
  const reload = useCallback(async () => {
    if (path) setData(await api<T>(path));
  }, [api, path]);
  return { data, setData, error, setError, reload };
}
