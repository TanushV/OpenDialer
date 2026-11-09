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
 * - an empty queue never claims to be "finished" while a call is open;
 * - R9E.8: the rep's own Hang up, Stay and outcome key show at once whatever the last re-read said: a used
 *   control never comes back, and only one save can be in flight.
 */
import { describe, expect, it } from "vitest";
import { CALL_STATES, TERMINAL_STATES } from "../src/state/callStates";
import {
  keys,
  screen,
  withIntent,
  type Blocker,
  type Intent,
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
            for (const saving of [null, "not_interested"])
              yield { calling, connecting, blockers, finished, call, saving };
}

/** Plain checks with a message built only on failure keep these exhaustive passes fast under coverage. */
function law(ok: boolean, name: string, context: () => unknown) {
  if (!ok) expect.fail(`${name}: ${JSON.stringify(context())}`);
}

const INTENTS: Intent[] = [
  { hangup: true },
  { stay: true },
  { hangup: true, stay: true },
];

describe("the Dialer screen over every input (R9E.2)", () => {
  it("obeys the calling laws in every combination", () => {
    let checked = 0;
    const DIGITS = "Digit1,Digit2,Digit3,Digit4,Digit5,Digit6";
    for (const input of inputs()) {
      const phase = screen(input);
      const available = keys(phase);
      const actions = Object.values(available);
      const names = Object.keys(available);
      const call = input.call;
      const open = call && !TERMINAL_STATES.has(call.state);
      const unsaved = call && call.state === "ENDED" && !call.outcome;
      const saving =
        (call?.state === "ENDED" || call?.state === "ENDING") && !!input.saving;
      const context = () => ({ input, phase });

      // A way forward: a key, a control on screen, or a save already answering.
      const control =
        (phase.kind === "paused" && phase.blocker !== null) ||
        phase.kind === "finished" ||
        saving;
      law(names.length > 0 || control, "no way forward", context);

      // A blocker on screen names its fix, and a self-clearing one that got stuck says what to press.
      if (phase.kind === "paused" && phase.blocker) {
        law(
          phase.blocker.message.length > 0,
          "a blocker without words",
          context,
        );
        if (phase.blocker.stuck)
          law(
            /Reconnect/.test(phase.blocker.message),
            "stuck without Reconnect",
            context,
          );
      }

      // Pause is reachable whenever calling is on and no one is on the line.
      if (
        input.calling &&
        !input.blockers.length &&
        ["connecting", "dialing", "saved", "skipped"].includes(phase.kind)
      )
        law(actions.includes("pause"), "Pause not reachable", context);

      // An unsaved ended call is saved with 1 to 6 before anything else happens, one save at a time.
      if (unsaved && !saving)
        law(
          phase.kind === "ended" && [...names].sort().join(",") === DIGITS,
          "an unsaved call cannot be saved with 1 to 6",
          context,
        );
      if (saving)
        law(
          phase.kind === "ended" &&
            phase.saving === "not_interested" &&
            names.length === 0,
          "a second save is offered while one is answering",
          context,
        );

      // A blocker stops the loop: no countdown and no dial while it is there (a call on screen still finishes).
      if (input.blockers.length && !open && call?.state !== "ENDED")
        law(
          phase.kind === "paused" || phase.kind === "connecting",
          "a blocker did not stop the loop",
          context,
        );

      // The queue is never "finished" while a call is open or unsaved.
      if (phase.kind === "finished")
        law(!(open || unsaved), "finished while a call is open", context);
      checked++;
    }
    expect(checked).toBeGreaterThan(10_000);
  });

  it("shows the rep's own key at once over any re-read, and never offers a used control again (R9E.8)", () => {
    let checked = 0;
    for (const input of inputs()) {
      const call = input.call;
      if (!call) continue;
      const open = !TERMINAL_STATES.has(call.state);
      for (const intent of INTENTS) {
        const shown = withIntent(call, intent);
        const phase = screen({ ...input, call: shown });
        const available = Object.values(keys(phase));
        const context = () => ({ input, intent, phase });
        // The intent belongs to this call: once it has finished, the next call's phase is its own.
        if (intent.hangup && open) {
          law(
            !available.includes("hangup") && !available.includes("skip"),
            "Hang up or Skip offered again",
            context,
          );
          // The carrier's own word on who hung up wins; otherwise the rep ended it.
          const who = call.provider_hangup_source === "callee" ? "they" : "you";
          law(
            phase.kind === "ended" && phase.who === who,
            "the screen does not say the call ended",
            context,
          );
          law(
            !!call.provider_hangup_source ||
              shown.provider_hangup_source === "caller",
            "the rep's hang-up is not shown as theirs",
            context,
          );
        }
        if (intent.stay && !intent.hangup)
          law(!available.includes("stay"), "Stay offered again", context);
        // The overlay never invents an outcome or changes a finished call.
        law(shown.outcome === call.outcome, "the outcome changed", context);
        if (!open)
          law(shown.state === call.state, "a finished call changed", context);
        checked++;
      }
    }
    expect(checked).toBeGreaterThan(10_000);
  });
});
