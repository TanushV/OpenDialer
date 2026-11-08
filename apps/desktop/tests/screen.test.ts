import { describe, expect, it } from "vitest";
import {
  headline,
  keys,
  screen,
  type PhaseKind,
  type ScreenInput,
} from "../src/calling/screen";
import { CALL_STATES } from "../src/state/callStates";

const base: ScreenInput = {
  calling: true,
  connecting: false,
  blockers: [],
  call: null,
  finished: null,
};
const blocker = {
  code: "NO_CAMPAIGN",
  message: "Import a lead list.",
  fix: "leads",
};
const kind = (input: Partial<ScreenInput>) =>
  screen({ ...base, ...input }).kind;

describe("the Dialer screen phases (section 6)", () => {
  it.each<[string, Partial<ScreenInput>, PhaseKind]>([
    ["not started", { calling: false }, "paused"],
    ["blocked", { blockers: [blocker] }, "paused"],
    ["opening audio", { connecting: true }, "connecting"],
    ["dialing", { call: { state: "DIALING" } }, "dialing"],
    ["ringing", { call: { state: "RINGING" } }, "dialing"],
    [
      "mailbox",
      { call: { state: "VOICEMAIL_GREETING" } },
      "voicemail_skipping",
    ],
    [
      "mailbox, rep stays",
      { call: { state: "VOICEMAIL_GREETING", stay_on_voicemail: true } },
      "talking",
    ],
    [
      "final attempt mailbox",
      { call: { state: "VOICEMAIL_READY", voicemail_turn: true } },
      "voicemail_final",
    ],
    ["person", { call: { state: "HUMAN" } }, "talking"],
    ["screening", { call: { state: "SCREENING" } }, "talking"],
    ["hanging up", { call: { state: "ENDING" } }, "ended"],
    ["owes an outcome", { call: { state: "ENDED" } }, "ended"],
    [
      "rep saved",
      { call: { state: "FINALIZED", outcome: "not_interested" } },
      "saved",
    ],
    [
      "carrier no answer",
      {
        call: {
          state: "FINALIZED",
          outcome: "no_answer",
          outcome_source: "carrier",
        },
      },
      "skipped",
    ],
    [
      "saved, then paused",
      { calling: false, call: { state: "FINALIZED", outcome: "right_party" } },
      "paused",
    ],
    [
      "saved, then a blocker",
      {
        blockers: [blocker],
        call: { state: "FINALIZED", outcome: "right_party" },
      },
      "paused",
    ],
    ["queue empty", { finished: { next_eligible_at: null } }, "finished"],
  ])("%s", (_, input, expected) => {
    expect(kind(input)).toBe(expected);
  });

  it("gives every call state a phase without throwing", () => {
    for (const state of CALL_STATES) {
      const phase = screen({ ...base, call: { state, outcome: "busy" } });
      expect(headline(phase)).toBeTruthy();
      expect(keys(phase)).toBeTypeOf("object");
    }
  });

  it("names who hung up and how long the call lasted", () => {
    const phase = screen({
      ...base,
      call: {
        state: "ENDED",
        provider_hangup_source: "callee",
        connected_seconds: 23,
      },
    });
    expect(headline(phase)).toBe("They hung up · 0:23");
    expect(
      headline(
        screen({ ...base, call: { state: "ENDING", connected_seconds: 61 } }),
      ),
    ).toBe("You ended the call · 1:01");
  });

  it("maps each phase's keys as in the behavior table", () => {
    const at = (input: Partial<ScreenInput>) =>
      keys(screen({ ...base, ...input }));
    expect(at({ calling: false })).toEqual({ Space: "start" });
    expect(at({ blockers: [blocker] })).toEqual({});
    expect(at({ call: { state: "RINGING" } })).toEqual({
      Space: "skip",
      KeyP: "pause",
    });
    expect(at({ call: { state: "VOICEMAIL_GREETING" } })).toEqual({
      Space: "skip",
      KeyS: "stay",
    });
    expect(at({ call: { state: "HUMAN" } })).toEqual({
      Escape: "hangup",
      KeyM: "mute",
    });
    expect(Object.keys(at({ call: { state: "ENDED" } }))).toEqual([
      "Digit1",
      "Digit2",
      "Digit3",
      "Digit4",
      "Digit5",
      "Digit6",
    ]);
    expect(
      at({ call: { state: "FINALIZED", outcome: "right_party" } }),
    ).toEqual({
      Space: "dial_now",
      KeyP: "pause",
      KeyC: "change_outcome",
    });
  });
});
