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
  /** Promised to clear by itself, was repaired and is still here: Reconnect is shown. */
  stuck?: boolean;
}

/** The call fields the screen reads. */
export interface ScreenCall {
  state: CallState | string;
  outcome?: string | null;
  outcome_source?: string | null;
  answered_at?: string | null;
  /** The rep ended this call with End as voicemail. */
  rep_voicemail_at?: string | null;
  bridged_at?: string | null;
  provider_hangup_source?: string | null;
  connected_seconds?: number | null;
}

export interface ScreenInput {
  /** The rep chose Start calling and has not paused. */
  calling: boolean;
  /** An outcome key was pressed and its save has not answered yet (R9E.8). */
  saving?: string | null;
  /** The rep's audio leg is being opened. */
  connecting: boolean;
  blockers: Blocker[];
  call: ScreenCall | null;
  /** The queue has no one to call now; the next eligible time when known. */
  finished: QueueStatus | null;
}

export interface QueueStatus {
  next_eligible_at: string | null;
  reason?: string;
  fix?: string;
  nextAction?: string;
}

export type Phase =
  | { kind: "paused"; blocker: Blocker | null }
  | { kind: "connecting" }
  | { kind: "dialing"; ringing: boolean }
  | {
      kind: "talking";
      /** No detection: an answer is "connected" until a person is confirmed (work package section 6). */
      answer?: "connected" | "person";
    }
  | {
      kind: "ended";
      who: "they" | "you" | null;
      seconds: number | null;
      /** The outcome being saved now; no other key acts until it answers. */
      saving?: string | null;
      /** Ended with End as voicemail: the rep says whether a message was left. */
      voicemail?: boolean;
    }
  | { kind: "saved"; outcome: string }
  | ({ kind: "finished" } & QueueStatus);

export type PhaseKind = Phase["kind"];

const DIALING = new Set([
  "AUTHORIZED",
  "RECONCILING",
  "DIAL_UNCONFIRMED",
  "DIALING",
]);

/**
 * What the rep just did, shown at once until the server's own record says the same (R9E.8). A re-read that left
 * before the server saw the rep's key can no longer bring back a control the rep already used.
 */
export interface Intent {
  /** Hang up or Skip was pressed. */
  hangup?: boolean;
}

export function withIntent<T extends ScreenCall>(
  call: T,
  intent: Intent | null,
): T {
  if (!intent) return call;
  let shown = call;
  if (intent.hangup) {
    if (!TERMINAL_STATES.has(shown.state) && shown.state !== "ENDING")
      shown = { ...shown, state: "ENDING" };
    if (!shown.provider_hangup_source)
      shown = { ...shown, provider_hangup_source: "caller" };
  }
  return shown;
}

export function screen(input: ScreenInput): Phase {
  const { call } = input;
  if (call && !TERMINAL_STATES.has(call.state)) {
    const phase = live(call);
    // An outcome pressed while the call is still ending shows as being saved (R9E.8).
    return phase.kind === "ended"
      ? { ...phase, saving: input.saving ?? null }
      : phase;
  }
  if (call && call.state !== "FINALIZED")
    return { ...ended(call), saving: input.saving ?? null };
  // Opening the rep's audio shows as such, even before presence says Ready.
  if (input.connecting) return { kind: "connecting" };
  // Pausing or a blocker stops the loop before the next call can be requested.
  if (!input.calling || input.blockers.length)
    return { kind: "paused", blocker: input.blockers[0] ?? null };
  if (input.finished) return { kind: "finished", ...input.finished };
  if (call?.outcome) return { kind: "saved", outcome: call.outcome };
  return { kind: "dialing", ringing: false };
}

function live(call: ScreenCall): Phase {
  if (call.state === "ENDING") return ended(call);
  if (call.state === "HUMAN") return { kind: "talking", answer: "person" };
  // Older calls may still carry a mailbox or screening state; they read as connected, like any answer.
  if (
    call.state === "ANSWERED_UNCLASSIFIED" ||
    call.state === "SCREENING" ||
    call.state === "VOICEMAIL_GREETING" ||
    call.state === "VOICEMAIL_READY" ||
    call.answered_at ||
    call.bridged_at
  )
    return { kind: "talking", answer: "connected" };
  if (DIALING.has(call.state)) return { kind: "dialing", ringing: false };
  if (call.state === "RINGING") return { kind: "dialing", ringing: true };
  return { kind: "talking" };
}

function ended(call: ScreenCall): Extract<Phase, { kind: "ended" }> {
  const who =
    call.provider_hangup_source === "callee"
      ? "they"
      : call.provider_hangup_source === "caller" || call.state === "ENDING"
        ? "you"
        : null;
  return {
    kind: "ended",
    who,
    seconds: call.connected_seconds ?? null,
    voicemail: Boolean(call.rep_voicemail_at),
  };
}

export type Action =
  | "start"
  | "pause"
  | "skip"
  | "voicemail"
  | "voicemail_left"
  | "voicemail_none"
  | "hangup"
  | "mute"
  | "record_consent"
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
      return { Space: "skip", Escape: "hangup", KeyP: "pause" };
    case "talking":
      // Space is End as voicemail after answer; Esc stays the plain Hang up (work package 6.4).
      // R: the rep told the prospect the call is recorded (call insights plan P1).
      return {
        Space: "voicemail",
        Escape: "hangup",
        KeyM: "mute",
        KeyR: "record_consent",
      };
    case "ended":
      // One save at a time: while it answers, no key can start another.
      return phase.saving
        ? {}
        : {
            ...Object.fromEntries(
              [1, 2, 3, 4, 5, 6].map((n) => [`Digit${n}`, { save: n }]),
            ),
            ...(phase.voicemail
              ? { KeyL: "voicemail_left", KeyN: "voicemail_none" }
              : {}),
          };
    case "saved":
      return { Space: "dial_now", KeyP: "pause", KeyC: "change_outcome" };
    case "finished":
      return { Space: "dial_now", KeyP: "pause" };
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
    case "talking":
      return {
        connected: "Connected",
        person: "Talking",
        unknown: "Talking",
      }[phase.answer ?? "unknown"];
    case "ended": {
      if (phase.saving) return "Saving the outcome";
      if (phase.voicemail) return "Voicemail · did you leave a message?";
      const who =
        phase.who === "they"
          ? "They hung up"
          : phase.who === "you"
            ? "You ended the call"
            : "Call ended";
      return phase.seconds == null ? who : `${who} · ${clock(phase.seconds)}`;
    }
    case "saved":
      return "Saved";
    case "finished":
      return phase.reason || "No one left to call right now";
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
