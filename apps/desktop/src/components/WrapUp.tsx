import { useRef, useState } from "react";
import { Check } from "lucide-react";
import { localEdit } from "../api/client";
import { label, useWorkspace } from "../context";
import type { Call } from "../types";
import {
  chooseZone,
  zoneCaption,
  zonedInput,
  zonedInstant,
} from "../state/zones";

/** One click covers most calls; every backend outcome stays under "More outcomes". */
const PRIMARY: [string, string][] = [
  ["no_answer", "No answer"],
  ["voicemail", "Voicemail"],
  ["gatekeeper", "Gatekeeper / wrong person"],
  ["not_interested", "Talked: not interested"],
  ["callback_requested", "Talked: callback requested"],
  ["meeting_booked", "Meeting booked"],
];
const OUTCOMES = [
  "right_party",
  "qualified_conversation",
  "sale_won",
  "callback_requested",
  "meeting_booked",
  "meeting_held",
  "gatekeeper",
  "screening",
  "ivr",
  "voicemail",
  "no_answer",
  "busy",
  "wrong_party",
  "invalid_endpoint",
  "not_interested",
  "opt_out",
  "temporary_failure",
];
const SUPPRESSES = new Set(["wrong_party", "invalid_endpoint", "opt_out"]);
// Carrier results are facts; anything else waits for the rep's choice.
const CARRIER: Record<string, string> = {
  NO_ANSWER: "no_answer",
  BUSY: "busy",
  TEMPORARY_FAILURE: "temporary_failure",
};
const primary = new Set(PRIMARY.map(([code]) => code));

export function WrapUp({
  call,
  timezone,
  onSaved,
}: {
  call: Call;
  /** The lead's explicit IANA zone for the callback time. */
  timezone?: string | null;
  onSaved: (call: Call) => void;
}) {
  const { api, run, notice } = useWorkspace();
  const zone = chooseZone(timezone);
  const [outcome, setOutcome] = useState(
    (call.state === "FINALIZED" && call.outcome) ||
      call.disposition_input?.outcome ||
      CARRIER[call.state] ||
      "",
  );
  const [more, setMore] = useState(!!outcome && !primary.has(outcome));
  const [contactRole, setContactRole] = useState(
    call.disposition_input?.contact_role || "unknown",
  );
  const [callbackAt, setCallbackAt] = useState(
    zonedInput(call.disposition_input?.callback_at, zone.zone),
  );
  const [wrapNote, setWrapNote] = useState(
    call.disposition_input?.callback_note || "",
  );
  const [saving, setSaving] = useState(false);
  const pending = useRef<{ fingerprint: string; request_id: string } | null>(
    null,
  );
  function choose(code: string) {
    setOutcome(code);
    if (!primary.has(code)) setMore(true);
  }
  async function save() {
    if (saving || !outcome) return;
    const body = {
      outcome,
      contact_role: contactRole,
      callback_note: wrapNote,
      expected_version: call.version,
      // Background bookkeeping bumps call.version; only rep edits move edit_version.
      expected_edit_version: call.edit_version ?? 0,
      callback_at:
        outcome === "callback_requested" && callbackAt
          ? zonedInstant(callbackAt, zone.zone)
          : null,
    };
    const fingerprint = JSON.stringify(body);
    if (pending.current?.fingerprint !== fingerprint)
      pending.current = { fingerprint, request_id: crypto.randomUUID() };
    setSaving(true);
    try {
      const result = await localEdit<Call>(
        api,
        `/v1/calls/${call.id}/disposition`,
        "POST",
        { ...body, request_id: pending.current.request_id },
      );
      notice(
        outcome === "meeting_booked" || outcome === "meeting_held"
          ? "Meeting recorded as rep-reported. Outreach is paused."
          : "Outcome saved. Analysis continues in the background.",
      );
      // The saved outcome is final; later refresh work reports its own errors.
      onSaved(result);
    } finally {
      setSaving(false);
    }
  }
  return (
    <section className="wrap-up card" aria-label="Wrap-up">
      <div>
        <span className="eyebrow">LEAVE A CLEAR NEXT STEP</span>
        <h3>How did the conversation go?</h3>
      </div>
      {call.suggested_outcome && (
        <div>
          <p>
            Suggested outcome: {label(call.suggested_outcome.code)}.{" "}
            {call.suggested_outcome.reason}
          </p>
          <button
            className="secondary"
            onClick={() => {
              choose(call.suggested_outcome!.code);
              setContactRole(call.suggested_outcome!.contact_role);
            }}
          >
            Use suggested outcome
          </button>
          <small>Review the outcome and contact role before saving.</small>
        </div>
      )}
      <div className="outcome-choices" role="group" aria-label="Outcome">
        {PRIMARY.map(([code, text]) => (
          <button
            key={code}
            type="button"
            className={outcome === code ? "chip selected" : "chip"}
            aria-pressed={outcome === code}
            onClick={() => choose(code)}
          >
            {text}
          </button>
        ))}
      </div>
      <details
        open={more}
        onToggle={(event) => setMore(event.currentTarget.open)}
      >
        <summary>More outcomes</summary>
        <label>
          Disposition
          <select value={outcome} onChange={(e) => choose(e.target.value)}>
            <option value="" disabled>
              Choose an outcome
            </option>
            {OUTCOMES.map((o) => (
              <option key={o} value={o}>
                {label(o)}
                {SUPPRESSES.has(o) ? " · stops calls to this number" : ""}
              </option>
            ))}
          </select>
        </label>
      </details>
      {outcome === "callback_requested" && (
        <label>
          Confirmed callback time ({zoneCaption(zone)})
          <input
            type="datetime-local"
            value={callbackAt}
            onChange={(e) => setCallbackAt(e.target.value)}
          />
        </label>
      )}
      <label>
        Who did you speak with?
        <select
          value={contactRole}
          onChange={(event) => setContactRole(event.target.value)}
        >
          <option value="unknown">Not confirmed</option>
          <option value="right_party">Target contact</option>
          <option value="gatekeeper">Gatekeeper</option>
          <option value="wrong_party">Wrong person</option>
        </select>
      </label>
      <label>
        Wrap-up note
        <textarea
          value={wrapNote}
          maxLength={1000}
          onChange={(event) => setWrapNote(event.target.value)}
        />
      </label>
      {call.rundown_status && (
        <p role="status">
          Rundown: {label(call.rundown_status)}. You can continue eligible
          calling while analysis finishes.
        </p>
      )}
      <button
        className="primary"
        disabled={saving || !outcome}
        onClick={() => void run(save)}
      >
        Save outcome
        <Check size={15} />
      </button>
    </section>
  );
}

/** A saved outcome whose callback time a person's edit or closure kept from applying. */
export function CallbackReviewNotice({
  review,
  onDismiss,
}: {
  review: NonNullable<Call["callback_review"]>;
  onDismiss: () => void;
}) {
  const { openTask } = useWorkspace();
  const first = review.task_ids[0];
  return (
    <section
      className="card padded callback-review"
      aria-label="Callback needs review"
    >
      <p role="status">
        Outcome saved. The callback time was not applied: {review.reason}
      </p>
      <div className="button-row">
        {first && openTask && (
          <button className="secondary small" onClick={() => openTask(first)}>
            {review.task_ids.length > 1
              ? "Review follow-ups"
              : "Review follow-up"}
          </button>
        )}
        <button className="text-button small" onClick={onDismiss}>
          Dismiss
        </button>
      </div>
    </section>
  );
}
