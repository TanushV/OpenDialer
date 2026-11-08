/**
 * The Dialer screen as a pure function of one state (section 6, R5.4).
 *
 * `screen()` names the phase the rep is in; `keys()` names what each key does in that phase. Nothing
 * else decides what the call view shows, so a phase cannot be half-drawn from two sources.
 */
import type { CallState } from "../state/callStates";
import { TERMINAL_STATES } from "../state/callStates";

export interface Blocker {
  code: string;
  message: string;
  fix: string;
}

/** The call fields the screen reads. */
export interface ScreenCall {
  state: CallState | string;
  outcome?: string | null;
  outcome_source?: string | null;
  voicemail_turn?: boolean;
  stay_on_voicemail?: boolean;
  machine_detected?: boolean;
  provider_hangup_source?: string | null;
  connected_seconds?: number | null;
}

export interface ScreenInput {
  /** The rep chose Start calling and has not paused. */
  calling: boolean;
  /** The rep's audio leg is being opened. */
  connecting: boolean;
  blockers: Blocker[];
  call: ScreenCall | null;
  /** The queue has no one to call now; the next eligible time when known. */
  finished: { next_eligible_at: string | null } | null;
}

export type Phase =
  | { kind: "paused"; blocker: Blocker | null }
  | { kind: "connecting" }
  | { kind: "dialing"; ringing: boolean }
  | { kind: "voicemail_skipping" }
  | { kind: "voicemail_final" }
  | { kind: "talking" }
  | { kind: "ended"; who: "they" | "you" | null; seconds: number | null }
  | { kind: "saved"; outcome: string; countdown_ms: number }
  | { kind: "skipped"; outcome: string; countdown_ms: number }
  | { kind: "finished"; next_eligible_at: string | null };

export type PhaseKind = Phase["kind"];

/** Seconds before the next call starts by itself (section 6). */
export const SAVED_COUNTDOWN_MS = 3000;
export const SKIPPED_COUNTDOWN_MS = 1500;
/** Outcomes where the call never reached a person; saved by the carrier or by the rep skipping. */
export const CARRIER_OUTCOMES = new Set([
  "no_answer",
  "busy",
  "voicemail",
  "temporary_failure",
]);

const DIALING = new Set([
  "AUTHORIZED",
  "RECONCILING",
  "DIAL_UNCONFIRMED",
  "DIALING",
]);
const MAILBOX = new Set(["VOICEMAIL_GREETING", "VOICEMAIL_READY"]);

export function screen(input: ScreenInput): Phase {
  const { call } = input;
  if (call && !TERMINAL_STATES.has(call.state)) return live(call);
  if (call && call.state !== "FINALIZED") return ended(call);
  // Opening the rep's audio shows as such, even before presence says Ready.
  if (input.connecting) return { kind: "connecting" };
  // Pausing or a blocker stops the loop before any countdown runs.
  if (!input.calling || input.blockers.length)
    return { kind: "paused", blocker: input.blockers[0] ?? null };
  if (call?.outcome) {
    // No answer, busy, a skipped mailbox or a carrier failure: nobody to talk about, so a shorter wait.
    const skipped = CARRIER_OUTCOMES.has(call.outcome);
    return skipped
      ? {
          kind: "skipped",
          outcome: call.outcome,
          countdown_ms: SKIPPED_COUNTDOWN_MS,
        }
      : {
          kind: "saved",
          outcome: call.outcome,
          countdown_ms: SAVED_COUNTDOWN_MS,
        };
  }
  if (input.finished)
    return {
      kind: "finished",
      next_eligible_at: input.finished.next_eligible_at,
    };
  return { kind: "dialing", ringing: false };
}

function live(call: ScreenCall): Phase {
  if (call.state === "ENDING") return ended(call);
  if (DIALING.has(call.state)) return { kind: "dialing", ringing: false };
  if (call.state === "RINGING") return { kind: "dialing", ringing: true };
  if (call.voicemail_turn) return { kind: "voicemail_final" };
  if (MAILBOX.has(call.state) && !call.stay_on_voicemail)
    return { kind: "voicemail_skipping" };
  return { kind: "talking" };
}

function ended(call: ScreenCall): Phase {
  const who =
    call.provider_hangup_source === "callee"
      ? "they"
      : call.provider_hangup_source === "caller" || call.state === "ENDING"
        ? "you"
        : null;
  return { kind: "ended", who, seconds: call.connected_seconds ?? null };
}

export type Action =
  | "start"
  | "pause"
  | "skip"
  | "stay"
  | "hangup"
  | "mute"
  | "dial_now"
  | "change_outcome"
  | { save: number };

/** What each key does in a phase (section 6 table). Keys not listed do nothing. */
export function keys(phase: Phase): Record<string, Action> {
  switch (phase.kind) {
    case "paused":
      return phase.blocker ? {} : { Space: "start" };
    case "connecting":
      return { KeyP: "pause" };
    case "dialing":
      return { Space: "skip", KeyP: "pause" };
    case "voicemail_skipping":
      return { Space: "skip", KeyS: "stay" };
    case "voicemail_final":
      return { Escape: "hangup" };
    case "talking":
      return { Escape: "hangup", KeyM: "mute" };
    case "ended":
      return Object.fromEntries(
        [1, 2, 3, 4, 5, 6].map((n) => [`Digit${n}`, { save: n }]),
      );
    case "saved":
      return { Space: "dial_now", KeyP: "pause", KeyC: "change_outcome" };
    case "skipped":
      return { Space: "dial_now", KeyP: "pause" };
    case "finished":
      return {};
    default:
      return unreachable(phase);
  }
}

/** The one line the status bar shows for a phase. */
export function headline(phase: Phase): string {
  switch (phase.kind) {
    case "paused":
      return phase.blocker ? phase.blocker.message : "Start calling";
    case "connecting":
      return "Connecting audio";
    case "dialing":
      return phase.ringing ? "Ringing" : "Dialing";
    case "voicemail_skipping":
      return "Voicemail: skipping";
    case "voicemail_final":
      return "Leave your message";
    case "talking":
      return "Talking";
    case "ended": {
      const who =
        phase.who === "they"
          ? "They hung up"
          : phase.who === "you"
            ? "You ended the call"
            : "Call ended";
      return phase.seconds == null ? who : `${who} · ${clock(phase.seconds)}`;
    }
    case "saved":
      return "Saved. Next call in 3 seconds";
    case "skipped":
      return "Skipped. Next call in a moment";
    case "finished":
      return "No one left to call right now";
    default:
      return unreachable(phase);
  }
}

export function clock(seconds: number) {
  const total = Math.max(0, Math.round(seconds));
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

function unreachable(value: never): never {
  throw new Error(`Unhandled phase ${JSON.stringify(value)}`);
}
