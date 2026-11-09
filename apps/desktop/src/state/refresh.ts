/** One read in flight; a request made during it requires a fresh read before publication. */
export function coalesceRefresh<T>(
  load: () => Promise<T>,
  publish: (value: T) => void,
) {
  let running: Promise<void> | null = null;
  let dirty = false;
  return (): Promise<void> => {
    dirty = true;
    if (!running) {
      running = (async () => {
        do {
          dirty = false;
          try {
            const value = await load();
            if (!dirty) publish(value);
          } catch (error) {
            if (!dirty) throw error;
          }
        } while (dirty);
      })().finally(() => {
        running = null;
      });
    }
    return running;
  };
}

// Fields that change on every read without changing anything shown.
const VOLATILE = new Set(["event_cursor"]);

/**
 * Keep every part of the shown workspace a re-read did not change, so screens that read it do not redraw, reload or
 * lose what the rep was doing. A re-read that changed nothing shown returns the shown workspace itself.
 */
export function shareUnchanged<T extends object>(shown: T | null, next: T): T {
  if (!shown) return next;
  const before = shown as Record<string, unknown>;
  const after = next as Record<string, unknown>;
  const merged: Record<string, unknown> = {};
  let changed = Object.keys(before).length !== Object.keys(after).length;
  for (const [key, value] of Object.entries(after)) {
    if (key in before && JSON.stringify(before[key]) === JSON.stringify(value))
      merged[key] = before[key];
    else {
      merged[key] = value;
      if (!VOLATILE.has(key)) changed = true;
    }
  }
  return changed ? (merged as T) : shown;
}
