import { describe, expect, it } from 'vitest';
import { applyEvents, makeState } from '../src/state/events';
import { OptOutJournal } from '../src/state/optouts';

class MemoryStorage {
  values = new Map<string, string>();
  getItem(k: string) { return this.values.get(k) ?? null; }
  setItem(k: string, v: string) { this.values.set(k, v); }
  removeItem(k: string) { this.values.delete(k); }
}

describe('durable offline opt-outs', () => {
  it('persists pending events through a restart and blocks calls until each acknowledgement', async () => {
    const storage = new MemoryStorage();
    const first = new OptOutJournal(storage, 'tenant-a');
    first.add('+14155550101', 'opt_out');
    const restarted = new OptOutJournal(storage, 'tenant-a');
    expect(restarted.blocksCalling).toBe(true);
    await restarted.synchronize(async () => { throw new Error('offline'); });
    expect(restarted.blocksCalling).toBe(true);
    await restarted.synchronize(async event => ({ id: event.id }));
    expect(restarted.blocksCalling).toBe(false);
  });
  it('does not expose another workspace journal', () => {
    const storage = new MemoryStorage();
    new OptOutJournal(storage, 'tenant-a').add('+14155550101', 'opt_out');
    expect(new OptOutJournal(storage, 'tenant-b').blocksCalling).toBe(false);
  });
});

describe('reconnect and stale event handling', () => {
  it('deduplicates events and rejects old entity versions', () => {
    const event = { event_id: 'event-1', sequence: 1, tenant_id: 'a', type: 'call.updated',
      entity_id: 'call', entity_version: 3, occurred_at: '2026-10-14T18:00:00Z', payload: { state: 'ENDED' } };
    let state = applyEvents(makeState('a'), [event, event]);
    state = applyEvents(state, [{ ...event, event_id: 'event-2', sequence: 2,
      entity_version: 2, payload: { state: 'RINGING' } }]);
    expect(state.entities.call.payload.state).toBe('ENDED');
    expect(state.cursor).toBe(2);
    expect(state.applied.size).toBe(2);
  });
  it('discards another tenant event', () => {
    const state = applyEvents(makeState('a'), [{ event_id: 'x', sequence: 1, tenant_id: 'b',
      type: 'coach.card_ready', entity_id: 'c', entity_version: 1, payload: {}, occurred_at: '' }]);
    expect(Object.keys(state.entities)).toHaveLength(0);
  });
});
