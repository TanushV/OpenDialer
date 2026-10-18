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
