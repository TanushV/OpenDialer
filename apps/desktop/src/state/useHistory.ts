import { useEffect, useRef, useState } from "react";
import { useWorkspace } from "../context";
import type { HistoryPage } from "../types";

/** Bounded reads; newer searches and authority changes discard late results. */
export function useHistory<T extends { id: string; version?: number }>(
  section: string,
  initial: T[],
) {
  const { api, snapshot, session } = useWorkspace();
  const [loaded, setLoaded] = useState<HistoryPage<T> | null>(null);
  const [busy, setBusy] = useState(false);
  const generation = useRef(0);
  const identity = `${snapshot.mode}:${snapshot.simulation}:${session.tenant_id}:${session.seller_id}:${session.user_id}:${session.access_token}`;
  const current = useRef(identity);
  current.current = identity;
  useEffect(() => {
    setLoaded(null);
    setBusy(false);
    return () => {
      generation.current++;
    };
  }, [identity]);
  const preview = snapshot.pages?.[section];
  const initialById = new Map(initial.map((row) => [row.id, row]));
  const items =
    loaded?.items.map((row) => {
      const fresh = initialById.get(row.id);
      return fresh &&
        fresh.version != null &&
        row.version != null &&
        fresh.version > row.version
        ? fresh
        : row;
    }) || initial;
  const cursor = loaded ? loaded.next_cursor : preview?.next_cursor;
  const total = loaded ? loaded.total : preview?.total;
  async function load(append = false, query = "", filter = "all") {
    const request = ++generation.current;
    const scope = current.current;
    setBusy(true);
    try {
      const params = new URLSearchParams({
        q: append ? loaded?.query || "" : query,
        filter: append ? loaded?.filter || "all" : filter,
      });
      if (append && cursor) params.set("cursor", cursor);
      const result = await api<HistoryPage<T>>(
        `/v1/workspace/${section}?${params}`,
      );
      if (request !== generation.current || scope !== current.current) return;
      const merged = append
        ? new Map(items.map((row) => [row.id, row]))
        : new Map<string, T>();
      result.items.forEach((row) => merged.set(row.id, row));
      setLoaded({
        ...result,
        query: params.get("q") || "",
        filter: params.get("filter") || "all",
        items: [...merged.values()],
      });
    } finally {
      if (request === generation.current && scope === current.current)
        setBusy(false);
    }
  }
  function update(value: T) {
    setLoaded((previous) =>
      previous
        ? {
            ...previous,
            items: previous.items.map((row) =>
              row.id === value.id ? { ...row, ...value } : row,
            ),
          }
        : previous,
    );
  }
  return {
    items,
    total,
    cursor,
    busy,
    load,
    update,
    query: loaded?.query || "",
    filter: loaded?.filter || "all",
  };
}
