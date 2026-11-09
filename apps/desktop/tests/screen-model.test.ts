/**
 * R9E.2: every combination of what the Dialer reads is checked against the calling laws, not a handful of cases.
 *
 * Laws, for every reachable input:
 * - a phase is always produced (no unhandled state);
 * - the rep always has a way forward: a key in this phase, or a control on screen (a blocker's fix, or Pause
 *   while the queue is empty, which also re-reads by itself);
 * - Pause is reachable whenever calling is on and no one is on the line;
 * - a call that ended unsaved can always be saved with 1 to 6, and nothing dials until it is;
 * - a blocker always stops the loop: no countdown, no dial;
 * - an empty queue never claims to be "finished" while a call is open.
 */
import { describe, expect, it } from "vitest";
import { CALL_STATES, TERMINAL_STATES } from "../src/state/callStates";
import {
  keys,
  screen,
  type Blocker,
  type ScreenCall,
  type ScreenInput,
} from "../src/calling/screen";

const BLOCKERS: Blocker[][] = [
  [],
  [
    {
      code: "CONNECTION_RECONNECTING",
      message: "Reconnecting.",
      fix: "dialer",
    },
  ],
  [
    {
      code: "TELNYX_NOT_CONFIGURED",
      message: "Add your key.",
      fix: "settings",
    },
  ],
  [
    {
      code: "CONNECTION_DELIVERY_GAP",
      message: "Did not clear. Press Reconnect.",
      fix: "settings",
      stuck: true,
    },
  ],
];
const FINISHED: ScreenInput["finished"][] = [
  null,
  { next_eligible_at: null, reason: "Everyone left is outside calling hours." },
  { next_eligible_at: "2026-11-10T13:00:00Z" },
];

function* calls(): Generator<ScreenCall | null> {
  yield null;
  for (const state of CALL_STATES)
    for (const outcome of [null, "no_answer", "not_interested"])
      for (const voicemail_turn of [false, true])
        for (const stay_on_voicemail of [false, true])
          for (const provider_hangup_source of [null, "callee", "caller"])
            yield {
              state,
              outcome,
              voicemail_turn,
              stay_on_voicemail,
              provider_hangup_source,
              connected_seconds: 12,
            };
}

function* inputs(): Generator<ScreenInput> {
  for (const calling of [false, true])
    for (const connecting of [false, true])
      for (const blockers of BLOCKERS)
        for (const finished of FINISHED)
          for (const call of calls())
            yield { calling, connecting, blockers, finished, call };
}

describe("the Dialer screen over every input (R9E.2)", () => {
  it("obeys the calling laws in every combination", () => {
    let checked = 0;
    for (const input of inputs()) {
      const phase = screen(input);
      const available = keys(phase);
      const call = input.call;
      const open = call && !TERMINAL_STATES.has(call.state);
      const unsaved = call && call.state === "ENDED" && !call.outcome;
      const where = JSON.stringify({ input, phase });

      // A way forward: a key, or a control on screen.
      const control =
        (phase.kind === "paused" && phase.blocker !== null) ||
        phase.kind === "finished";
      expect(Object.keys(available).length > 0 || control, where).toBe(true);

      // A blocker on screen names its fix, and a self-clearing one that got stuck says what to press.
      if (phase.kind === "paused" && phase.blocker) {
        expect(phase.blocker.message.length > 0, where).toBe(true);
        if (phase.blocker.stuck)
          expect(phase.blocker.message, where).toMatch(/Reconnect/);
      }

      // Pause is reachable whenever calling is on and no one is on the line.
      if (
        input.calling &&
        !input.blockers.length &&
        ["connecting", "dialing", "saved", "skipped"].includes(phase.kind)
      )
        expect(Object.values(available), where).toContain("pause");

      // An unsaved ended call is saved with 1 to 6 before anything else happens.
      if (unsaved) {
        expect(phase.kind, where).toBe("ended");
        expect(Object.keys(available).sort(), where).toEqual(
          ["Digit1", "Digit2", "Digit3", "Digit4", "Digit5", "Digit6"].sort(),
        );
      }

      // A blocker stops the loop: no countdown and no dial while it is there (a call on screen still finishes).
      if (input.blockers.length && !open && call?.state !== "ENDED")
        expect(["paused", "connecting"], where).toContain(phase.kind);

      // The queue is never "finished" while a call is open or unsaved.
      if (phase.kind === "finished") expect(open || unsaved, where).toBeFalsy();
      checked++;
    }
    expect(checked).toBeGreaterThan(10_000);
  });
});
