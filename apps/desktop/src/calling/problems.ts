import { ApiError } from "../api/client";
import { isTerminal, type Call } from "../types";

export type CallProblem = {
  title: string;
  message: string;
  fix?: string | null;
  nextAction?: string;
};

export function startProblem(error: unknown, test: boolean): CallProblem {
  const reason =
    error instanceof TypeError
      ? "The app could not reach its local calling service."
      : error instanceof Error
        ? error.message
        : String(error);
  return {
    title: test ? "Test call did not start" : "Call did not start",
    message: reason,
    ...(error instanceof ApiError
      ? { fix: error.fix, nextAction: error.nextAction }
      : {}),
  };
}

export function unconfirmedProblem(
  test: boolean,
  message: string,
): CallProblem {
  return {
    title: test
      ? "Test call could not be confirmed"
      : "Call could not be confirmed",
    message,
    fix: "attention",
    nextAction: "Check the original attempt before trying again.",
  };
}

const TEST_HANGUP: Record<string, string> = {
  user_busy: "Telnyx reported that the destination was busy.",
  no_answer: "Telnyx reported that the destination did not answer.",
  timeout: "The carrier’s ring timeout elapsed without an answer.",
  unallocated_number:
    "The carrier reported that the destination number is not assigned.",
  no_route_destination:
    "The carrier reported that it had no route to the destination.",
  call_rejected: "The destination or its carrier rejected the call.",
  originator_cancel:
    "The calling side cancelled before the destination answered.",
};

export function callProblem(call: Call | null): CallProblem | null {
  if (!call) return null;
  const test = Boolean(call.is_test);
  if (call.dial_error)
    return {
      title: test ? "Test call did not start" : "Call did not start",
      ...call.dial_error,
    };
  if (
    call.end_reason === "dial_not_sent" ||
    call.end_reason === "unsent_intent_cancelled"
  )
    return {
      title: test ? "Test call did not start" : "Call did not start",
      message:
        call.end_reason === "dial_not_sent"
          ? "The local dial request expired before it was sent to Telnyx."
          : "The pending dial was cancelled before it was sent to Telnyx.",
    };
  if (call.end_reason === "consent_revoked_before_submission")
    return {
      title: test ? "Test call did not start" : "Call did not start",
      message: "A number hold was recorded before the dial could be sent.",
      fix: test ? "settings" : "attention",
    };
  if (
    call.state === "RECONCILING" ||
    call.state === "DIAL_UNCONFIRMED" ||
    call.end_reason === "recovery_timeout"
  )
    return unconfirmedProblem(
      test,
      call.recovery_reason ||
        "Telnyx did not confirm the dial. The call may have been placed.",
    );
  if (call.sip_block_code)
    return {
      title: test
        ? "Test call blocked by the carrier"
        : "Call blocked by the carrier",
      message: `The carrier refused this caller ID (SIP ${call.sip_block_code}).`,
      fix: "numbers",
    };
  if (
    !test ||
    !isTerminal(call) ||
    call.answered_at ||
    call.connected_seconds != null
  )
    return null;
  const cause = call.provider_hangup_cause;
  if (!cause && call.end_reason !== "call.hangup") return null;
  return {
    title: "Test call did not connect",
    message: cause
      ? (TEST_HANGUP[cause] ??
        `Telnyx ended the call before reporting an answer (reason: ${cause}).`)
      : "Telnyx ended the call without reporting why or confirming an answer.",
  };
}
