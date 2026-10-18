import { describe, expect, it, vi } from 'vitest';
import { coalesceRefresh } from './refresh';

function pending<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

describe('workspace refresh after a mutation', () => {
  it('reruns after an overlapping save and never publishes the stale response', async () => {
    const first = pending<string>();
    const second = pending<string>();
    const load = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const publish = vi.fn();
    const refresh = coalesceRefresh(load, publish);
    const a = refresh();
    const b = refresh();
    const c = refresh();
    expect(load).toHaveBeenCalledTimes(1);
    first.resolve('paused before rep clicked ready');
    await first.promise;
    await Promise.resolve();
    expect(load).toHaveBeenCalledTimes(2);
    expect(publish).not.toHaveBeenCalled();
    second.resolve('ready');
    await Promise.all([a,b,c]);
    expect(publish).toHaveBeenCalledExactlyOnceWith('ready');
  });

  it('releases the in-flight state after failure so a subsequent request can recover', async () => {
    const publish = vi.fn();
    const refresh = coalesceRefresh(vi.fn().mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce('ready'), publish);
    await expect(refresh()).rejects.toThrow('offline');
    await refresh();
    expect(publish).toHaveBeenCalledExactlyOnceWith('ready');
  });
});
