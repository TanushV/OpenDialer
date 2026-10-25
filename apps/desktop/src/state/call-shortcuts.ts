/** Space advances only from an idle calling surface, never while editing. */
export function canAdvanceWithSpace(
  event: Pick<
    KeyboardEvent,
    "code" | "repeat" | "altKey" | "ctrlKey" | "metaKey" | "shiftKey"
  >,
  state: {
    visible: boolean;
    ready: boolean;
    blocked: boolean;
    active: boolean;
    busy: boolean;
    wrapUpPending: boolean;
  },
  interactive: boolean,
) {
  return (
    event.code === "Space" &&
    !event.repeat &&
    !event.altKey &&
    !event.ctrlKey &&
    !event.metaKey &&
    !event.shiftKey &&
    !interactive &&
    state.visible &&
    state.ready &&
    !state.blocked &&
    !state.active &&
    !state.busy &&
    !state.wrapUpPending
  );
}
