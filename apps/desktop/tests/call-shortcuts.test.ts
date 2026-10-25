import { describe, expect, it } from "vitest";
import { canAdvanceWithSpace } from "../src/state/call-shortcuts";

describe("next scheduled call shortcut", () => {
  it("only advances an idle visible dialer without modifiers or repetition", () => {
    const state = {
      visible: true,
      ready: true,
      blocked: false,
      active: false,
      busy: false,
      wrapUpPending: false,
    };
    const event = {
      code: "Space",
      repeat: false,
      altKey: false,
      ctrlKey: false,
      metaKey: false,
      shiftKey: false,
    };
    expect(canAdvanceWithSpace(event, state, false)).toBe(true);
    for (const change of [
      { visible: false },
      { ready: false },
      { blocked: true },
      { active: true },
      { busy: true },
      { wrapUpPending: true },
    ])
      expect(canAdvanceWithSpace(event, { ...state, ...change }, false)).toBe(
        false,
      );
    expect(canAdvanceWithSpace(event, state, true)).toBe(false);
    for (const change of [
      { repeat: true },
      { ctrlKey: true },
      { metaKey: true },
      { altKey: true },
      { shiftKey: true },
      { code: "Enter" },
    ])
      expect(canAdvanceWithSpace({ ...event, ...change }, state, false)).toBe(
        false,
      );
  });
});
