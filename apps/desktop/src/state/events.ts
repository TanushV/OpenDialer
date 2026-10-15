export interface AppEvent {
  event_id: string;
  sequence: number;
  tenant_id: string;
  type: string;
  entity_id: string;
  entity_version: number;
  occurred_at: string;
  payload: Record<string, unknown>;
}
export interface EventState {
  tenant: string;
  cursor: number;
  applied: Set<string>;
  entities: Record<
    string,
    { version: number; payload: Record<string, unknown> }
  >;
}
export function makeState(tenant: string): EventState {
  return { tenant, cursor: 0, applied: new Set(), entities: {} };
}
export function applyEvents(state: EventState, events: AppEvent[]): EventState {
  const result = {
    ...state,
    applied: new Set(state.applied),
    entities: { ...state.entities },
  };
  for (const event of events) {
    if (event.tenant_id !== state.tenant || result.applied.has(event.event_id))
      continue;
    result.applied.add(event.event_id);
    result.cursor = Math.max(result.cursor, event.sequence);
    const previous = result.entities[event.entity_id];
    if (!previous || event.entity_version > previous.version)
      result.entities[event.entity_id] = {
        version: event.entity_version,
        payload: event.payload,
      };
  }
  return result;
}
